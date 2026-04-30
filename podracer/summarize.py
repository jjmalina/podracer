from dataclasses import dataclass

import httpx
from pydantic import BaseModel


DEFAULT_TIMEOUT = 600.0
DEFAULT_CTX = 65536
DEFAULT_MAX_TOKENS = 16384


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

Look for these clues:
- Hosts introducing themselves ("I'm Patrick Serezna and I'm Kevin Muir")
- Hosts introducing guests ("we welcome Craig Shapiro to the show")
- Speakers referring to each other by name ("Craig, thanks for coming")
- Self-identification ("I recently joined Ninja Trader")

For each speaker label that appears in the transcript, provide:
- Their real name (or a descriptive label like "Producer" if unknown)
- Their role or title if mentioned
- The timestamp and quote where they are identified

Be thorough — check the entire transcript for clues. Multiple labels may \
refer to the same person if the diarization split them."""

SUMMARY_PROMPT = """\
You are a podcast summarization assistant. You will be given a speaker key \
followed by a timestamped transcript. Write a concise summary (3-5 paragraphs) \
capturing the key topics discussed, main arguments or insights shared by each \
speaker, and any notable takeaways. Use real speaker names from the speaker key. \
Do not invent information not in the transcript."""

CHAPTERS_PROMPT = """\
You are a podcast chapter generator. You will be given a speaker key followed \
by a timestamped transcript. Break the episode into logical chapters or segments. \
Aim for 10-20 chapters that reflect natural topic transitions. Each chapter needs \
a short descriptive title, the timestamp (HH:MM:SS) where it begins, and a 1-2 \
sentence summary. Use real speaker names from the speaker key. Cover the entire \
episode from start to finish — do not skip any major section."""

INSIGHTS_PROMPT = """\
You are a podcast analyst. You will be given a speaker key followed by a \
timestamped transcript. Extract the key insights and takeaways — things a \
listener would want to remember or act on. For each insight, provide the text \
of the insight, the timestamp (HH:MM:SS) where it appears, and the real name \
of the speaker who expressed it (using the speaker key). Aim for 10-15 insights \
covering the full episode from start to finish."""

SPEAKER_TAKES_PROMPT = """\
You are a podcast analyst. You will be given a speaker key followed by a \
timestamped transcript. Identify unique opinions, theses, or takes that are \
specific to individual speakers — things that represent their distinct \
perspective rather than group consensus. For each take, provide the speaker's \
real name (using the speaker key), what they argued or claimed, and the \
timestamp (HH:MM:SS). Aim for 10-15 takes covering the full episode and \
all major speakers."""


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


class Insight(BaseModel):
    text: str
    timestamp: str
    speaker: str


class InsightList(BaseModel):
    insights: list[Insight]


class SpeakerTake(BaseModel):
    speaker: str
    take: str
    timestamp: str


class SpeakerTakeList(BaseModel):
    speaker_takes: list[SpeakerTake]


class PodcastSummary(BaseModel):
    summary: str
    speakers: list[SpeakerIdentification]
    chapters: list[Chapter]
    insights: list[Insight]
    speaker_takes: list[SpeakerTake]


def _extract_json(content: str) -> str:
    start = content.find("{")
    end = content.rfind("}") + 1
    if start != -1 and end > start:
        content = content[start:end]
    return content


def _repair_truncated_json(content: str) -> str:
    """Try to close truncated JSON arrays/objects."""
    import json
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass
    last_brace = content.rfind("}")
    if last_brace == -1:
        return content
    trimmed = content[:last_brace + 1]
    open_brackets = trimmed.count("[") - trimmed.count("]")
    open_braces = trimmed.count("{") - trimmed.count("}")
    trimmed += "]" * open_brackets + "}" * open_braces
    return trimmed


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
    return _extract_json(resp.json()["message"]["content"])


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
        import sys
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    data = resp.json()
    content = _extract_json(data["choices"][0]["message"]["content"])
    if data["choices"][0]["finish_reason"] == "length":
        import sys
        print("  warning: response truncated, attempting repair", file=sys.stderr)
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
    resp = httpx.post(
        f"{backend.base_url}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if not resp.is_success:
        import sys
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    data = resp.json()
    content = _extract_json(data["choices"][0]["message"]["content"])
    if data["choices"][0].get("finish_reason") == "length":
        import sys
        print("  warning: response truncated, attempting repair", file=sys.stderr)
        content = _repair_truncated_json(content)
    return content


def _chat(backend: Backend, system: str, user: str, schema: dict) -> str:
    if backend.name == "vllm":
        return _chat_vllm(backend, system, user, schema)
    if backend.name == "openrouter":
        return _chat_openrouter(backend, system, user, schema)
    return _chat_ollama(backend, system, user, schema)


def identify_speakers(transcript: str, backend: Backend) -> list[SpeakerIdentification]:
    content = _chat(
        backend, SPEAKER_ID_PROMPT,
        f"Identify every speaker in this transcript:\n\n{transcript}",
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
    for s in sorted(speakers, key=lambda x: len(x.label), reverse=True):
        result = result.replace(f"[{s.label}]", f"[{s.name}]")
    return result


def _step(label: str, func, *args):
    import sys
    import time
    print(f"[{label}] starting...", file=sys.stderr, flush=True)
    start = time.time()
    result = func(*args)
    elapsed = time.time() - start
    print(f"[{label}] done ({elapsed:.1f}s)", file=sys.stderr, flush=True)
    return result


def summarize(transcript: str, backend: Backend | None = None) -> PodcastSummary:
    """Summarize a transcript in multiple focused passes."""
    if backend is None:
        backend = Backend.ollama("gemma4:e4b")

    speakers = _step("speakers", identify_speakers, transcript, backend)
    named_transcript = rewrite_transcript(transcript, speakers)

    summary_content = _step(
        "summary", _chat,
        backend, SUMMARY_PROMPT,
        f"Summarize this transcript:\n\n{named_transcript}",
        Summary.model_json_schema(),
    )
    summary = Summary.model_validate_json(summary_content).summary

    chapters_content = _step(
        "chapters", _chat,
        backend, CHAPTERS_PROMPT,
        f"Generate chapters for this transcript:\n\n{named_transcript}",
        ChapterList.model_json_schema(),
    )
    chapters = ChapterList.model_validate_json(chapters_content).chapters

    insights_content = _step(
        "insights", _chat,
        backend, INSIGHTS_PROMPT,
        f"Extract insights from this transcript:\n\n{named_transcript}",
        InsightList.model_json_schema(),
    )
    insights = InsightList.model_validate_json(insights_content).insights

    takes_content = _step(
        "speaker_takes", _chat,
        backend, SPEAKER_TAKES_PROMPT,
        f"Extract unique speaker takes from this transcript:\n\n{named_transcript}",
        SpeakerTakeList.model_json_schema(),
    )
    speaker_takes = SpeakerTakeList.model_validate_json(takes_content).speaker_takes

    return PodcastSummary(
        summary=summary,
        speakers=speakers,
        chapters=chapters,
        insights=insights,
        speaker_takes=speaker_takes,
    )
