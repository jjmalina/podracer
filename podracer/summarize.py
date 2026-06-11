import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import structlog
from pydantic import BaseModel
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

from podracer import logger

DEFAULT_TIMEOUT = 600.0
DEFAULT_CTX = 131072
DEFAULT_MAX_TOKENS = 16384
CHAPTER_DETAIL_WORKERS = 5


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


def _log_llm_usage(backend_name: str, model: str, usage: TokenUsage) -> None:
    """Emit one structured ``llm_call`` event so token usage is aggregatable.

    Fields: backend, model, input_tokens, output_tokens, total_tokens.
    """
    logger.info("llm_call", backend=backend_name, model=model, **usage.model_dump())


def _chat_ollama(backend: Backend, system: str, user: str, schema: dict) -> str:
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
    _log_llm_usage("ollama", backend.model, TokenUsage.from_ollama(data))
    return _extract_json(data["message"]["content"])


def _count_tokens_vllm(backend: Backend, messages: list[dict]) -> int:
    resp = httpx.post(
        f"{backend.base_url}/v1/chat/completions",
        json={"model": backend.model, "messages": messages, "max_tokens": 1, "stream": False},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.is_success:
        return resp.json()["usage"]["prompt_tokens"]
    return 0


def _chat_vllm(backend: Backend, system: str, user: str, schema: dict) -> str:
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
    _log_llm_usage("vllm", backend.model, TokenUsage.from_openai(data))
    content = _extract_json(data["choices"][0]["message"]["content"])
    if data["choices"][0]["finish_reason"] == "length":
        logger.warning("Response truncated, attempting repair")
        content = _repair_truncated_json(content)
    return content


def _chat_openrouter(backend: Backend, system: str, user: str, schema: dict) -> str:
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
    _log_llm_usage("openrouter", backend.model, TokenUsage.from_openai(data))
    content = _extract_json(data["choices"][0]["message"]["content"])
    if data["choices"][0].get("finish_reason") == "length":
        logger.warning("Response truncated, attempting repair")
        content = _repair_truncated_json(content)
    return content


def _chat(backend: Backend, system: str, user: str, schema: dict) -> str:
    if backend.name == "vllm":
        return _chat_vllm(backend, system, user, schema)
    if backend.name == "openrouter":
        return _chat_openrouter(backend, system, user, schema)
    return _chat_ollama(backend, system, user, schema)


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
    content = _chat(
        backend, SPEAKER_ID_PROMPT,
        f"Identify every speaker in this transcript:\n\n{prefix}{transcript}",
        SpeakerIdentifications.model_json_schema(),
    )
    return SpeakerIdentifications.model_validate_json(content).speakers


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
    content = _chat(backend, CHAPTER_DETAIL_PROMPT, user, Summary.model_json_schema())
    return Summary.model_validate_json(content).summary


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
                logger.warning("chapter %d enrichment failed (%s); keeping short summary", i, e)
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

    summary_content = _step(
        "summary", _chat,
        backend, SUMMARY_PROMPT,
        f"Summarize this transcript:\n\n{notes_prefix}{named_transcript}",
        Summary.model_json_schema(),
    )
    summary = Summary.model_validate_json(summary_content).summary

    chapters_content = _step(
        "chapters", _chat,
        backend, CHAPTERS_PROMPT,
        f"Generate chapters for this transcript:\n\n{notes_prefix}{named_transcript}",
        ChapterList.model_json_schema(),
    )
    chapters = ChapterList.model_validate_json(chapters_content).chapters

    chapters = _step("chapter_detail", enrich_chapters, chapters, named_transcript, speakers, backend)

    highlights_content = _step(
        "highlights", _chat,
        backend, HIGHLIGHTS_PROMPT,
        f"Extract highlights from this transcript:\n\n{notes_prefix}{named_transcript}",
        HighlightList.model_json_schema(),
    )
    highlights = HighlightList.model_validate_json(highlights_content).highlights

    return PodcastSummary(
        summary=summary,
        speakers=speakers,
        chapters=chapters,
        highlights=highlights,
    )
