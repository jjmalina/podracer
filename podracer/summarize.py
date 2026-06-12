import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

from podracer import logger

DEFAULT_TIMEOUT = 600.0
DEFAULT_CTX = 131072
DEFAULT_MAX_TOKENS = 16384
CHAPTER_DETAIL_WORKERS = 5

# --- LLM output quality guards ---------------------------------------------
# See docs/plans/2026-06-12-llm-output-quality-guards.md. deepseek-v4-flash via
# OpenRouter intermittently returns degenerate completions — stub text that
# stops mid-sentence (finish_reason "stop", schema-valid JSON), or prose instead
# of JSON — that every existing check passes. They are transient, so we validate
# content plausibility after each call and retry the call when it fails.
_MAX_LLM_ATTEMPTS = 3              # 1 initial attempt + 2 retries
_RETRY_BACKOFF = 0.5              # seconds between degenerate-output retries
_MIN_SUMMARY_CHARS = 200          # an episode summary (3-5 paragraphs) shorter than this is a stub
_MIN_HIGHLIGHTS = 5              # prompt asks for 15-25; <5 on a full episode is degenerate
_MIN_HIGHLIGHT_CHARS = 40        # an individual highlight shorter than this is a stub ("On GenAI: ")
_BAD_HIGHLIGHT_FRACTION = 0.2    # if >20% of items are degenerate, the whole response is suspect
_SUBSTANTIAL_SLICE_CHARS = 3000  # a chapter whose transcript slice is this long should get a real writeup
_MIN_CHAPTER_DETAIL_CHARS = 400  # ...so a sub-400-char detail for such a chapter is too thin (prompt asks 150-300 words)  # noqa: E501
_TERMINAL_PUNCT = (".", "!", "?", '"', ")", "”", "’", "…")


@dataclass
class Backend:
    name: str
    base_url: str
    model: str
    api_key: str | None = None

    @staticmethod
    def ollama(model: str, base_url: str = "http://localhost:11434") -> "Backend":
        return Backend(name="ollama", base_url=base_url, model=model)

    @staticmethod
    def vllm(model: str, base_url: str = "http://localhost:8000") -> "Backend":
        return Backend(name="vllm", base_url=base_url, model=model)

    @staticmethod
    def openrouter(model: str, api_key: str) -> "Backend":
        return Backend(
            name="openrouter",
            base_url="https://openrouter.ai/api",
            model=model,
            api_key=api_key,
        )


SPEAKER_ID_PROMPT = """\
You are given a timestamped podcast transcript where speakers are labeled as \
SPEAKER_00, SPEAKER_01, etc. Your job is to identify each speaker's real name.

If show notes or an episode description are provided, use them as the \
authoritative source for the correct spelling of all names. Transcription \
from audio frequently misspells names (e.g. "Wazenthal" instead of \
"Weisenthal", "Allaway" instead of "Alloway"). Always cross-reference \
names you hear in the transcript against the show notes and use the show \
notes spelling.

Also look for these clues in the transcript:
- Hosts introducing themselves ("I'm Patrick Ceresna and I'm Kevin Muir")
- Hosts introducing guests ("we welcome Craig Shapiro to the show")
- Speakers referring to each other by name ("Craig, thanks for coming")
- Self-identification ("I recently joined Ninja Trader")

Important rules:
- If multiple SPEAKER labels refer to the same person (common with diarization), \
merge them into a single entry. List all labels in the "label" field separated \
by commas (e.g. "SPEAKER_00, SPEAKER_03").
- Each real person should appear exactly once in your output.
- For advertisement/sponsor voices, set the role to "advertiser". This helps \
filter ads from the analysis.

For each unique speaker, provide:
- Their real name (or a descriptive label like "Advertiser" if unknown)
- Their role or title if mentioned (use "advertiser" for ad/sponsor voices)
- The timestamp and quote where they are identified"""

SUMMARY_PROMPT = """\
You are a podcast summarization assistant. You will be given a speaker key \
followed by a timestamped transcript. Write a concise summary of 3-5 short \
paragraphs (no more than 500 words total) capturing the key topics discussed, \
main arguments or insights shared by each speaker, and any notable takeaways. \
Use real speaker names from the speaker key. Do not quote or echo the \
transcript — synthesize the content in your own words. Do not invent \
information not in the transcript. \
Ignore advertisement and sponsor segments entirely."""

CHAPTERS_PROMPT = """\
You are a podcast chapter generator. You will be given a speaker key followed \
by a timestamped transcript. Break the episode into logical chapters or segments. \
Aim for 10-20 chapters that reflect natural topic transitions. Each chapter needs \
a short descriptive title, the timestamp (HH:MM:SS) where it begins, and a 1-2 \
sentence summary. Use real speaker names from the speaker key. Cover the entire \
episode from start to finish — do not skip any major section. \
Skip advertisement and sponsor segments — do not create chapters for ads.

Some episodes open with a teaser or cold-open montage: a rapid sequence of \
short clips pulled from later in the episode, played before the show formally \
begins. If you detect one, represent the ENTIRE teaser as a single chapter \
titled exactly "Teaser" at its starting timestamp. Do not create separate \
topic chapters for the clips inside the teaser — those topics get their own \
chapters where they are actually discussed later, and duplicating them here \
would misrepresent where each topic was covered."""

CHAPTER_DETAIL_PROMPT = """\
You are writing a detailed summary of ONE chapter of a podcast for a reader \
who hasn't listened. You will be given:
- The chapter title
- The speaker key for the episode
- The transcript segment for just this chapter

Write a substantive summary (typically 2-4 short paragraphs, 150-300 words) \
that explains what was discussed in enough depth that the reader learns \
something. Not "they talked about X" — actually convey the substance of X. \
Use \\n\\n between paragraphs.

Definitional asides: when speakers reference a technical concept, named \
entity, or piece of shared context that they don't fully explain, you may \
add a brief definition (1-2 clauses) drawing on your background knowledge. \
Phrase these asides so the reader can tell what the speakers said versus \
what you're explaining — e.g. "X is Y, ..." or "(X here refers to Y)" \
rather than putting the explanation in the speaker's mouth. If you aren't \
confident in a definition, omit it — let the speakers' words stand.

Do not invent quantitative claims (numbers, dates, prices, benchmarks, \
named studies, direct quotes) that aren't in the transcript.

If this chapter is genuinely short on substance — an intro greeting, a brief \
tangent — a 1-2 sentence summary is fine. Don't pad.

Use real speaker names from the speaker key. Synthesize in your own \
words — do not quote the transcript verbatim. Return only the summary \
prose — no title, no timestamp."""

HIGHLIGHTS_PROMPT = """\
You are a podcast analyst. You will be given a speaker key followed by a \
timestamped transcript. Extract the episode's highlights — the points a \
listener would want to remember. Each highlight is one of two kinds:
- "takeaway": a key fact, finding, or actionable point from the discussion.
- "opinion": a distinct opinion, thesis, or argument specific to one \
speaker — their particular perspective rather than group consensus.

For each highlight provide: the text, the kind (exactly "takeaway" or \
"opinion"), the timestamp (HH:MM:SS) where it appears, and the real name of \
the speaker who expressed it (using the speaker key). Do not record the same \
point twice — if something is both a notable takeaway and a speaker's opinion, \
pick the single kind that fits best and list it once. Aim for 15-25 highlights \
covering the full episode from start to finish, across all major speakers. \
Ignore advertisement and sponsor segments entirely."""


class SpeakerIdentification(BaseModel):
    label: str
    name: str
    role: str
    evidence_timestamp: str
    evidence_quote: str


class SpeakerIdentifications(BaseModel):
    speakers: list[SpeakerIdentification]


class Summary(BaseModel):
    summary: str


class Chapter(BaseModel):
    title: str
    timestamp: str
    summary: str


class ChapterList(BaseModel):
    chapters: list[Chapter]


class Highlight(BaseModel):
    text: str
    timestamp: str
    speaker: str
    kind: str  # "takeaway" or "opinion"


class HighlightList(BaseModel):
    highlights: list[Highlight]


# Legacy item models — retained so summaries stored before the insights/takes
# consolidation still deserialize. New summaries populate `highlights` instead.
class Insight(BaseModel):
    text: str
    timestamp: str
    speaker: str


class SpeakerTake(BaseModel):
    speaker: str
    take: str
    timestamp: str


class PodcastSummary(BaseModel):
    summary: str
    speakers: list[SpeakerIdentification]
    chapters: list[Chapter]
    highlights: list[Highlight] = []
    # Legacy fields, retained for reading pre-consolidation summaries.
    insights: list[Insight] = []
    speaker_takes: list[SpeakerTake] = []

    def effective_highlights(self) -> list[Highlight]:
        """Highlights to display, migrating legacy insights/takes on read."""
        if self.highlights:
            return self.highlights
        merged = [
            Highlight(text=i.text, timestamp=i.timestamp, speaker=i.speaker, kind="takeaway")
            for i in self.insights
        ]
        merged += [
            Highlight(text=t.take, timestamp=t.timestamp, speaker=t.speaker, kind="opinion")
            for t in self.speaker_takes
        ]
        return merged


def _extract_json(content: str) -> str:
    start = content.find("{")
    end = content.rfind("}") + 1
    if start != -1 and end > start:
        content = content[start:end]
    return content


def _repair_truncated_json(content: str) -> str:
    """Try to close truncated JSON strings/arrays/objects."""
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass
    content = content.rstrip()
    in_string = False
    escaped = False
    for ch in content:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        content += '"'
    open_brackets = content.count("[") - content.count("]")
    open_braces = content.count("{") - content.count("}")
    content += "]" * open_brackets + "}" * open_braces
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass
    last_brace = content.rfind("}")
    if last_brace == -1:
        return content
    return content[:last_brace + 1]


class DegenerateOutputError(Exception):
    """An LLM response that is schema-valid but implausible as content: a stub,
    an empty list, or text cut off mid-sentence. Raised by the validators below
    to trigger a retry of the call."""

    def __init__(self, step: str, reason: str, *, output_chars: int | None = None):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason
        self.output_chars = output_chars


def _ends_terminally(text: str) -> bool:
    """True if text ends in sentence-terminal punctuation. A stub like
    ``"Kelsey Hightower articulates a "`` fails this; a complete sentence
    (even one ending in a quote or paren) passes."""
    text = text.rstrip()
    return bool(text) and text.endswith(_TERMINAL_PUNCT)


def _check_speakers(m: "SpeakerIdentifications") -> None:
    if not m.speakers:
        raise DegenerateOutputError("speakers", "no speakers identified")


def _check_summary(m: "Summary") -> None:
    s = m.summary.strip()
    if len(s) < _MIN_SUMMARY_CHARS or not _ends_terminally(s):
        raise DegenerateOutputError("summary", f"stub or mid-sentence cut ({len(s)} chars)", output_chars=len(s))


def _check_chapters(m: "ChapterList") -> None:
    if len(m.chapters) < 2:
        raise DegenerateOutputError("chapters", f"only {len(m.chapters)} chapter(s)")
    if len({c.timestamp for c in m.chapters}) < 2:
        raise DegenerateOutputError("chapters", "chapters do not span multiple timestamps")


def _check_highlights(m: "HighlightList") -> None:
    """Drop stub items, then judge the surviving list. Mutates ``m.highlights``
    so an accepted response keeps only the good items (per the plan: drop bad
    items unless too many fail, in which case retry the whole call)."""
    good = [h for h in m.highlights
            if len(h.text.strip()) >= _MIN_HIGHLIGHT_CHARS and _ends_terminally(h.text)]
    total = len(m.highlights)
    dropped = total - len(good)
    m.highlights = good
    if len(good) < _MIN_HIGHLIGHTS:
        raise DegenerateOutputError(
            "highlights", f"only {len(good)} usable highlights (dropped {dropped} of {total})",
            output_chars=len(good))
    if total and dropped > _BAD_HIGHLIGHT_FRACTION * total:
        raise DegenerateOutputError(
            "highlights", f"{dropped} of {total} highlights degenerate", output_chars=len(good))


class TokenUsage(BaseModel):
    """Normalized LLM token counts across backends.

    All fields are optional: a backend may omit usage, and we'd rather log a
    null than crash. Counts are logged as JSON numbers so OpenSearch dynamic-maps
    them numeric (sum/avg work).
    """
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @staticmethod
    def from_openai(data: dict) -> "TokenUsage":
        """OpenAI-compatible response (OpenRouter, vLLM): the ``usage`` object."""
        usage = data.get("usage") or {}
        return TokenUsage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )

    @staticmethod
    def from_ollama(data: dict) -> "TokenUsage":
        """Ollama /api/chat response: prompt_eval_count / eval_count."""
        inp = data.get("prompt_eval_count")
        out = data.get("eval_count")
        total = inp + out if inp is not None and out is not None else None
        return TokenUsage(input_tokens=inp, output_tokens=out, total_tokens=total)


@dataclass
class ChatResult:
    """One chat completion plus the signals the quality guards inspect."""
    content: str
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _log_llm_usage(backend_name: str, model: str, usage: TokenUsage, *,
                   finish_reason: str | None = None,
                   native_finish_reason: str | None = None,
                   provider: str | None = None) -> None:
    """Emit one structured ``llm_call`` event so token usage is aggregatable.

    Beyond the token counts, logs ``finish_reason`` / ``native_finish_reason`` /
    ``provider`` so a degenerate (e.g. 14-token) completion is distinguishable
    from a healthy one in OpenSearch, and so a misbehaving provider is nameable.
    """
    logger.info("llm_call", backend=backend_name, model=model,
                finish_reason=finish_reason, native_finish_reason=native_finish_reason,
                provider=provider, **usage.model_dump())


def _chat_ollama(backend: Backend, system: str, user: str, schema: dict,
                 repair: bool = False) -> ChatResult:
    payload = {
        "model": backend.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "options": {"num_ctx": DEFAULT_CTX, "num_predict": DEFAULT_MAX_TOKENS},
    }
    payload["think"] = False
    resp = httpx.post(
        f"{backend.base_url}/api/chat",
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = TokenUsage.from_ollama(data)
    finish_reason = data.get("done_reason")
    _log_llm_usage("ollama", backend.model, usage, finish_reason=finish_reason)
    content = _extract_json(data["message"]["content"])
    if repair:
        content = _repair_truncated_json(content)
    return ChatResult(content=content, finish_reason=finish_reason,
                      input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


def _count_tokens_vllm(backend: Backend, messages: list[dict]) -> int:
    resp = httpx.post(
        f"{backend.base_url}/v1/chat/completions",
        json={"model": backend.model, "messages": messages, "max_tokens": 1, "stream": False},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.is_success:
        return resp.json()["usage"]["prompt_tokens"]
    return 0


def _chat_vllm(backend: Backend, system: str, user: str, schema: dict,
               repair: bool = False) -> ChatResult:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    input_tokens = _count_tokens_vllm(backend, messages)
    if input_tokens > 0:
        max_tokens = min(DEFAULT_MAX_TOKENS, DEFAULT_CTX - input_tokens - 64)
        max_tokens = max(max_tokens, 2048)
    else:
        max_tokens = DEFAULT_MAX_TOKENS
    payload = {
        "model": backend.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": schema},
        },
    }
    resp = httpx.post(
        f"{backend.base_url}/v1/chat/completions",
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    if not resp.is_success:
        logger.error("vLLM request failed: %s", resp.text)
    resp.raise_for_status()
    data = resp.json()
    usage = TokenUsage.from_openai(data)
    finish_reason = data["choices"][0].get("finish_reason")
    _log_llm_usage("vllm", backend.model, usage, finish_reason=finish_reason)
    content = _extract_json(data["choices"][0]["message"]["content"])
    if repair:
        content = _repair_truncated_json(content)
    return ChatResult(content=content, finish_reason=finish_reason,
                      input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


def _chat_openrouter(backend: Backend, system: str, user: str, schema: dict,
                     repair: bool = False) -> ChatResult:
    payload = {
        "model": backend.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": DEFAULT_MAX_TOKENS,
        "reasoning": {"effort": "none"},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": schema},
        },
        # Only route to providers that honor response_format.json_schema. This
        # kills most of the prose-instead-of-JSON responses at the source —
        # they came from providers that silently ignored the structured-output
        # request. (Pair with the retry below as a backstop.)
        "provider": {"require_parameters": True},
    }
    headers = {"Authorization": f"Bearer {backend.api_key}"}

    @retry(
        retry=retry_if_result(lambda r: r.status_code == 429),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=5, min=5, max=60),
        before_sleep=lambda rs: logger.warning("Rate limited, retrying (attempt %d/5)", rs.attempt_number),
    )
    def _post():
        return httpx.post(
            f"{backend.base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    resp = _post()
    if not resp.is_success:
        logger.error("OpenRouter request failed: %s", resp.text)
    resp.raise_for_status()
    data = resp.json()
    usage = TokenUsage.from_openai(data)
    choice = data["choices"][0]
    finish_reason = choice.get("finish_reason")
    native_finish_reason = choice.get("native_finish_reason")
    provider = data.get("provider")
    _log_llm_usage("openrouter", backend.model, usage, finish_reason=finish_reason,
                   native_finish_reason=native_finish_reason, provider=provider)
    content = _extract_json(choice["message"]["content"])
    if repair:
        content = _repair_truncated_json(content)
    return ChatResult(content=content, finish_reason=finish_reason,
                      native_finish_reason=native_finish_reason, provider=provider,
                      input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


def _chat(backend: Backend, system: str, user: str, schema: dict,
          repair: bool = False) -> ChatResult:
    if backend.name == "vllm":
        return _chat_vllm(backend, system, user, schema, repair)
    if backend.name == "openrouter":
        return _chat_openrouter(backend, system, user, schema, repair)
    return _chat_ollama(backend, system, user, schema, repair)


def _chat_checked[M: BaseModel](backend: Backend, system: str, user: str, model_cls: type[M],
                                check: Callable[[M], None], *,
                                prefer: Callable[[M], int] | None = None) -> tuple[M | None, bool]:
    """Call the LLM, validate schema *and* content, retrying on degeneracy.

    Returns ``(model, passed)``. ``model`` is the first response that passes
    both schema (Pydantic) and content (``check``) validation. If none do, it is
    the "best" failed response — chosen by ``prefer(model)`` (a sort key, e.g.
    summary length), defaulting to the last — or ``None`` if nothing ever
    parsed. ``check(model)`` raises :class:`DegenerateOutputError` on implausible
    content and may filter the model in place (highlights). ``finish_reason ==
    "length"`` alone is *not* a retry trigger: a complete, plausible answer that
    merely hit the token cap passes the content check, and a truncated one fails
    it (mid-sentence). JSON repair is attempted only on the final attempt, so it
    no longer masks truncation on the first response.
    """
    schema = model_cls.model_json_schema()
    best: M | None = None
    best_key: int | None = None
    for attempt in range(_MAX_LLM_ATTEMPTS):
        is_last = attempt == _MAX_LLM_ATTEMPTS - 1
        result = _chat(backend, system, user, schema, repair=is_last)
        model: M | None = None
        reason: str | None = None
        try:
            model = model_cls.model_validate_json(result.content)
        except ValidationError:
            reason = "invalid_json"  # prose-instead-of-JSON, or unrepairable truncation
        if model is not None:
            try:
                check(model)
            except DegenerateOutputError as de:
                reason = de.reason
        if model is not None and reason is None:
            return model, True
        # One warning per failed attempt. episode_id is attached automatically
        # via the contextvar bound in summarize_episode / the worker.
        logger.warning("llm_degenerate_output", attempt=attempt + 1,
                       max_attempts=_MAX_LLM_ATTEMPTS, reason=reason,
                       backend=backend.name, model=backend.model, provider=result.provider,
                       finish_reason=result.finish_reason,
                       input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        if model is not None:
            key = prefer(model) if prefer else attempt
            if best_key is None or key > best_key:
                best, best_key = model, key
        if not is_last:
            time.sleep(_RETRY_BACKOFF)
    return best, False


def _checked_or_fail[M: BaseModel](model_cls: type[M], backend: Backend, system: str, user: str,
                                   check: Callable[[M], None]) -> M:
    """:func:`_chat_checked` for an episode-level step. Accepts a best-effort
    response when retries are exhausted (better a degraded episode than a failed
    job), but raises — failing the job so the worker retries it — if nothing
    ever parsed, rather than storing a structurally broken episode."""
    model, passed = _chat_checked(backend, system, user, model_cls, check)
    if model is None:
        step = structlog.contextvars.get_contextvars().get("step", model_cls.__name__)
        raise DegenerateOutputError(step, "no valid response after retries")
    if not passed:
        logger.warning("llm_degenerate_output_exhausted", accepted=True)
    return model


def _build_context_prefix(podcast_description: str | None = None,
                          show_notes: str | None = None) -> str:
    parts = []
    if podcast_description:
        parts.append(f"PODCAST DESCRIPTION:\n{podcast_description}")
    if show_notes:
        parts.append(f"SHOW NOTES:\n{show_notes}")
    if parts:
        return "\n\n".join(parts) + "\n\nTRANSCRIPT:\n"
    return ""


def identify_speakers(transcript: str, backend: Backend,
                      show_notes: str | None = None,
                      podcast_description: str | None = None) -> list[SpeakerIdentification]:
    prefix = _build_context_prefix(podcast_description, show_notes)
    model = _checked_or_fail(
        SpeakerIdentifications, backend, SPEAKER_ID_PROMPT,
        f"Identify every speaker in this transcript:\n\n{prefix}{transcript}",
        _check_speakers,
    )
    return model.speakers


def format_speaker_key(speakers: list[SpeakerIdentification]) -> str:
    lines = ["SPEAKER KEY:"]
    for s in speakers:
        lines.append(
            f"- {s.label} = {s.name} ({s.role}), "
            f"identified at [{s.evidence_timestamp}]: \"{s.evidence_quote}\""
        )
    return "\n".join(lines)


def rewrite_transcript(transcript: str, speakers: list[SpeakerIdentification]) -> str:
    result = transcript
    replacements: list[tuple[str, str]] = []
    for s in speakers:
        for label in (part.strip() for part in s.label.split(",")):
            replacements.append((label, s.name))
    for label, name in sorted(replacements, key=lambda x: len(x[0]), reverse=True):
        result = result.replace(f"[{label}]", f"[{name}]")
    return result


def _step(label: str, func, *args):
    # Bind `step` as a contextvar so every event emitted underneath (llm_call,
    # llm_degenerate_output, ...) is tagged with which pass produced it —
    # including the chapter-detail ThreadPoolExecutor, which captures these
    # contextvars and re-binds them in its worker threads.
    with structlog.contextvars.bound_contextvars(step=label):
        logger.info("[%s] starting...", label)
        start = time.time()
        result = func(*args)
        elapsed = time.time() - start
        logger.info("[%s] done (%.1fs)", label, elapsed)
        return result


_TS_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def _slice_transcript_by_chapter(named_transcript: str, start_ts: str, end_ts: str) -> str:
    """Return transcript lines whose [HH:MM:SS] timestamp falls in [start_ts, end_ts)."""
    kept: list[str] = []
    for line in named_transcript.splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        ts = m.group(1)
        if start_ts <= ts < end_ts:
            kept.append(line)
    return "\n".join(kept)


def _is_teaser_chapter(chapter: "Chapter") -> bool:
    """A cold-open/teaser chapter splices clips from later in the episode, so
    its slice is a montage. Enriching it would inflate those brief clips into
    detail that duplicates the chapters where the topics are actually covered."""
    title = chapter.title.lower()
    return "teaser" in title or "cold open" in title or "cold-open" in title


def _enrich_one_chapter(backend: Backend, speaker_key: str, chapter: "Chapter", slice_text: str) -> str:
    user = (
        f"CHAPTER TITLE: {chapter.title}\n\n"
        f"{speaker_key}\n\n"
        f"TRANSCRIPT SEGMENT FOR THIS CHAPTER:\n{slice_text}"
    )

    def check(m: "Summary") -> None:
        s = m.summary.strip()
        if not _ends_terminally(s):
            raise DegenerateOutputError("chapter_detail", "stub or mid-sentence cut", output_chars=len(s))
        if len(slice_text) > _SUBSTANTIAL_SLICE_CHARS and len(s) < _MIN_CHAPTER_DETAIL_CHARS:
            raise DegenerateOutputError(
                "chapter_detail", f"thin ({len(s)} chars) for a substantial chapter", output_chars=len(s))

    model, passed = _chat_checked(backend, CHAPTER_DETAIL_PROMPT, user, Summary, check,
                                  prefer=lambda m: len(m.summary))
    if passed and model is not None:
        return model.summary
    # Retries exhausted. Never downgrade the reader's experience: keep the best
    # enrichment we got if it has more substance than the existing chapters-pass
    # summary, otherwise fall back to that summary (the original behavior).
    best = model.summary if model is not None else ""
    kept = "enrichment" if len(best) > len(chapter.summary) else "short_summary"
    logger.warning("chapter_enrichment_fallback", chapter_title=chapter.title, kept=kept,
                   enrichment_chars=len(best), short_summary_chars=len(chapter.summary))
    return best if kept == "enrichment" else chapter.summary


def enrich_chapters(chapters: list["Chapter"], named_transcript: str,
                    speakers: list[SpeakerIdentification], backend: Backend) -> list["Chapter"]:
    """Replace each chapter's summary with a substantive per-chapter writeup.

    Fans out one LLM call per chapter via a thread pool (calls are HTTP I/O bound).
    Falls back to the original short summary if a per-chapter call fails.
    """
    if not chapters:
        return chapters
    speaker_key = format_speaker_key(speakers)
    sentinel = "99:99:99"
    # contextvars bound on the calling thread (e.g. the worker's episode_id /
    # job_id) don't propagate into ThreadPoolExecutor threads, so capture them
    # and re-bind inside each task — otherwise the per-chapter llm_call events
    # (the bulk of token usage) would lose that context.
    log_context = structlog.contextvars.get_contextvars()

    def task(i: int) -> tuple[int, str]:
        with structlog.contextvars.bound_contextvars(**log_context):
            if _is_teaser_chapter(chapters[i]):
                return i, chapters[i].summary
            start = chapters[i].timestamp
            end = chapters[i + 1].timestamp if i + 1 < len(chapters) else sentinel
            slice_text = _slice_transcript_by_chapter(named_transcript, start, end)
            if not slice_text:
                return i, chapters[i].summary
            try:
                return i, _enrich_one_chapter(backend, speaker_key, chapters[i], slice_text)
            except Exception as e:
                # Degenerate output is handled inside _enrich_one_chapter; this
                # catches transport/unexpected errors so one chapter can't sink
                # the whole episode.
                logger.warning("chapter_enrichment_error", chapter=i, error=str(e))
                return i, chapters[i].summary

    workers = min(CHAPTER_DETAIL_WORKERS, len(chapters))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, new_summary in ex.map(task, range(len(chapters))):
            chapters[i].summary = new_summary
    return chapters


def summarize(transcript: str, backend: Backend | None = None,
              show_notes: str | None = None,
              podcast_description: str | None = None) -> PodcastSummary:
    """Summarize a transcript in multiple focused passes."""
    if backend is None:
        backend = Backend.ollama("gemma4:e4b")

    speakers = _step("speakers", identify_speakers, transcript, backend, show_notes, podcast_description)
    named_transcript = rewrite_transcript(transcript, speakers)

    notes_prefix = _build_context_prefix(podcast_description, show_notes)

    summary = _step(
        "summary", _checked_or_fail,
        Summary, backend, SUMMARY_PROMPT,
        f"Summarize this transcript:\n\n{notes_prefix}{named_transcript}",
        _check_summary,
    ).summary

    chapters = _step(
        "chapters", _checked_or_fail,
        ChapterList, backend, CHAPTERS_PROMPT,
        f"Generate chapters for this transcript:\n\n{notes_prefix}{named_transcript}",
        _check_chapters,
    ).chapters

    chapters = _step("chapter_detail", enrich_chapters, chapters, named_transcript, speakers, backend)

    highlights = _step(
        "highlights", _checked_or_fail,
        HighlightList, backend, HIGHLIGHTS_PROMPT,
        f"Extract highlights from this transcript:\n\n{notes_prefix}{named_transcript}",
        _check_highlights,
    ).highlights

    return PodcastSummary(
        summary=summary,
        speakers=speakers,
        chapters=chapters,
        highlights=highlights,
    )
