"""One eval per prompt: how to run the production pass for a case, what the
judge should see, and the free structural metrics.

Downstream passes take *frozen* upstream inputs from the prod-stored summary
(the speaker key for summary/chapters/highlights, the chapter list for
chapter_detail) so each eval measures one prompt in isolation — a model's
chapter writeups are not penalized for its speaker-ID mistakes. Every eval
calls the same functions the worker calls, with the same content checks.
"""
import contextvars
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from podracer.evals.dataset import EvalCase
from podracer.models import Chapter, ChapterList, Highlight, HighlightList, SpeakerIdentification, is_ad_speaker
from podracer.summarize import (
    CHAPTER_DETAIL_PROMPT,
    CHAPTER_DETAIL_WORKERS,
    CHAPTERS_PROMPT,
    HIGHLIGHTS_PROMPT,
    SPEAKER_ID_PROMPT,
    SUMMARY_PROMPT,
    Backend,
    DegenerateOutputError,
    SpeakerIdentifications,
    Summary,
    _build_context_prefix,
    _check_chapter_detail,
    _check_chapters,
    _check_highlights,
    _check_speakers,
    _check_summary,
    _enrich_one_chapter,
    _is_teaser_chapter,
    chapter_slice,
    format_speaker_key,
    generate_chapters,
    generate_highlights,
    identify_speakers,
    rewrite_transcript,
    write_summary,
)
from podracer.timestamps import usable_timeline_end


@dataclass
class Item:
    """One generated unit of an eval: the episode output, or one chapter for
    chapter_detail. ``source`` and ``instructions`` are what the judge sees."""
    key: str                      # "" for episode-level, "c03" for chapter 3
    output: Any                   # the generated object(s), JSON-serializable via _dump
    output_text: str              # rendered for the judge
    source: str                   # the material the model was given (minus the instruction line)
    instructions: str             # the system prompt
    structural: dict = field(default_factory=dict)
    passed: bool = True           # production content check passed on the final output
    meta: dict = field(default_factory=dict)


class FrozenInputsMissing(RuntimeError):
    """The case has no prod summary to take frozen upstream inputs from."""


def _dump(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    return obj


def _words(text: str) -> int:
    return len(text.split())


def _passes(check, model) -> bool:
    try:
        check(model)
        return True
    except DegenerateOutputError:
        return False


def _frozen(case: EvalCase) -> tuple[list[SpeakerIdentification], str, str, int | None]:
    """(speakers, named_transcript, notes_prefix, transcript_end) from prod."""
    if case.prod_summary is None:
        raise FrozenInputsMissing(f"episode {case.episode_id} has no prod summary")
    speakers = case.prod_summary.speakers
    named = rewrite_transcript(case.transcript, speakers)
    prefix = _build_context_prefix(case.podcast_description, case.show_notes)
    return speakers, named, prefix, usable_timeline_end(named)


# --- speakers -------------------------------------------------------------

_LABEL_RE = re.compile(r"\[(SPEAKER_\d+)\]")


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _label_metrics(case: EvalCase, speakers: list[SpeakerIdentification]) -> dict:
    """Agreement with labels.json: name precision/recall over real (non-ad)
    people, and per-label mapping accuracy. Only as good as the labels — they
    are seeded from prod output until a human verifies them."""
    labels = (case.labels or {}).get("speakers")
    if not labels:
        return {}
    truth_names = {_norm_name(s["name"]) for s in labels if not _is_ad_label(s)}
    truth_map = {lab: _norm_name(s["name"]) for s in labels for lab in s.get("labels", [])}
    got_names = {_norm_name(s.name) for s in speakers if not is_ad_speaker(s)}
    got_map = {lab.strip(): _norm_name(s.name) for s in speakers for lab in s.label.split(",")}
    tp = len(truth_names & got_names)
    mapped = [lab for lab in truth_map if lab in got_map]
    correct = sum(1 for lab in mapped if got_map[lab] == truth_map[lab])
    return {
        "labels_verified": bool((case.labels or {}).get("verified")),
        "name_precision": tp / len(got_names) if got_names else 0.0,
        "name_recall": tp / len(truth_names) if truth_names else 0.0,
        "label_accuracy": correct / len(truth_map) if truth_map else 0.0,
    }


def _is_ad_label(entry: dict) -> bool:
    return is_ad_speaker(SpeakerIdentification(label="", name=entry.get("name", ""), role=entry.get("role", ""),
                                               evidence_timestamp="", evidence_quote=""))


def run_speakers(backend: Backend, case: EvalCase, **_kw: Any) -> list[Item]:
    speakers = identify_speakers(case.transcript, backend, case.show_notes, case.podcast_description)
    prefix = _build_context_prefix(case.podcast_description, case.show_notes)
    transcript_labels = set(_LABEL_RE.findall(case.transcript))
    assigned = {lab.strip() for s in speakers for lab in s.label.split(",")}
    structural = {
        "n_speakers": len(speakers),
        "n_advertisers": sum(1 for s in speakers if is_ad_speaker(s)),
        "n_transcript_labels": len(transcript_labels),
        "label_coverage": (len(transcript_labels & assigned) / len(transcript_labels)) if transcript_labels else 1.0,
        **_label_metrics(case, speakers),
    }
    return [Item(
        key="", output=speakers, output_text=format_speaker_key(speakers),
        source=f"{prefix}{case.transcript}", instructions=SPEAKER_ID_PROMPT,
        structural=structural, passed=_passes(_check_speakers, SpeakerIdentifications(speakers=speakers)),
    )]


# --- summary --------------------------------------------------------------

def run_summary(backend: Backend, case: EvalCase, **_kw: Any) -> list[Item]:
    _, named, prefix, _ = _frozen(case)
    text = write_summary(named, backend, prefix)
    structural = {
        "chars": len(text), "words": _words(text),
        "paragraphs": len([p for p in text.split("\n\n") if p.strip()]),
    }
    return [Item(
        key="", output=text, output_text=text, source=f"{prefix}{named}", instructions=SUMMARY_PROMPT,
        structural=structural, passed=_passes(_check_summary, Summary(summary=text)),
    )]


# --- chapters -------------------------------------------------------------

def _render_chapters(chapters: list[Chapter]) -> str:
    return "\n".join(f"[{c.timestamp}] {c.title}: {c.summary}" for c in chapters)


def run_chapters(backend: Backend, case: EvalCase, **_kw: Any) -> list[Item]:
    _, named, prefix, end = _frozen(case)
    if end is None:
        raise FrozenInputsMissing(f"episode {case.episode_id} has no usable timeline")
    chapters = generate_chapters(named, backend, end, prefix)
    starts = [c.seconds for c in chapters if c.seconds is not None]
    structural = {
        "n_chapters": len(chapters),
        "first_start_s": starts[0] if starts else None,
        "last_start_s": starts[-1] if starts else None,
        "coverage_ratio": (starts[-1] / end) if starts and end else 0.0,
        "duplicate_starts": len(starts) - len(set(starts)),
        "has_teaser": any(_is_teaser_chapter(c) for c in chapters),
        "mean_gap_min": ((starts[-1] - starts[0]) / 60 / (len(starts) - 1)) if len(starts) > 1 else None,
    }
    return [Item(
        key="", output=chapters, output_text=_render_chapters(chapters),
        source=f"{prefix}{named}", instructions=CHAPTERS_PROMPT, structural=structural,
        passed=_passes(lambda m: _check_chapters(m, end), ChapterList(chapters=chapters)),
    )]


# --- chapter_detail -------------------------------------------------------

def _sample_indices(n: int, k: int | None) -> list[int]:
    """Evenly spaced chapter indices so a cap still samples the whole episode."""
    if not k or k >= n:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)}) if k > 1 else [n // 2]


def run_chapter_detail(backend: Backend, case: EvalCase, chapters_per_episode: int | None = None,
                       **_kw: Any) -> list[Item]:
    speakers, named, _, _ = _frozen(case)
    assert case.prod_summary is not None
    # Blank summaries: _enrich_one_chapter's no-downgrade fallback compares
    # against the existing summary, and prod's is already an enrichment.
    chapters = [Chapter(title=c.title, timestamp=c.timestamp, summary="") for c in case.prod_summary.chapters]
    speaker_key = format_speaker_key(speakers)
    candidates = [i for i in range(len(chapters))
                  if not _is_teaser_chapter(chapters[i]) and chapter_slice(chapters, i, named)]
    chosen = [candidates[j] for j in _sample_indices(len(candidates), chapters_per_episode)]

    def one(i: int) -> Item:
        slice_text = chapter_slice(chapters, i, named)
        text = _enrich_one_chapter(backend, speaker_key, chapters[i], slice_text)
        source = (f"CHAPTER TITLE: {chapters[i].title}\n\n{speaker_key}\n\n"
                  f"TRANSCRIPT SEGMENT FOR THIS CHAPTER:\n{slice_text}")
        return Item(
            key=f"c{i:02d}", output=text, output_text=text, source=source, instructions=CHAPTER_DETAIL_PROMPT,
            structural={"chars": len(text), "words": _words(text), "slice_chars": len(slice_text),
                        "substantial_slice": len(slice_text) > 3000},
            passed=bool(text) and _passes(lambda m: _check_chapter_detail(m, slice_text), Summary(summary=text)),
            meta={"chapter_index": i, "chapter_title": chapters[i].title, "timestamp": chapters[i].timestamp},
        )

    # Same fan-out as production. Copy the caller's context once per task (a
    # Context can't be entered by two threads at once) so the runner's usage
    # sink, a ContextVar, follows each task into its pool thread.
    contexts = [contextvars.copy_context() for _ in chosen]

    def in_context(ctx: contextvars.Context, i: int) -> Item:
        return ctx.run(one, i)

    with ThreadPoolExecutor(max_workers=min(CHAPTER_DETAIL_WORKERS, max(1, len(chosen)))) as ex:
        return list(ex.map(in_context, contexts, chosen))


# --- highlights -----------------------------------------------------------

def _render_highlights(highlights: list[Highlight]) -> str:
    return "\n".join(f"[{h.timestamp}] ({h.kind}) {h.speaker}: {h.text}" for h in highlights)


def _decile_coverage(highlights: list[Highlight], end: int) -> float:
    if not end:
        return 0.0
    bins = {min(9, int(10 * h.seconds / end)) for h in highlights if h.seconds is not None}
    return len(bins) / 10


def _dup_ratio(highlights: list[Highlight]) -> float:
    if not highlights:
        return 0.0
    keys = [_norm_name(h.text)[:60] for h in highlights]
    return 1 - len(set(keys)) / len(keys)


def run_highlights(backend: Backend, case: EvalCase, **_kw: Any) -> list[Item]:
    speakers, named, prefix, end = _frozen(case)
    if end is None:
        raise FrozenInputsMissing(f"episode {case.episode_id} has no usable timeline")
    highlights = generate_highlights(named, backend, end, prefix)
    names = {_norm_name(s.name) for s in speakers}
    structural = {
        "n_highlights": len(highlights),
        "n_takeaway": sum(1 for h in highlights if h.kind == "takeaway"),
        "n_opinion": sum(1 for h in highlights if h.kind == "opinion"),
        "decile_coverage": _decile_coverage(highlights, end),
        "attribution_known": (sum(1 for h in highlights if _norm_name(h.speaker) in names) / len(highlights))
        if highlights else 0.0,
        "dup_ratio": _dup_ratio(highlights),
        "mean_chars": (sum(len(h.text) for h in highlights) / len(highlights)) if highlights else 0,
    }
    return [Item(
        key="", output=highlights, output_text=_render_highlights(highlights),
        source=f"{prefix}{named}", instructions=HIGHLIGHTS_PROMPT, structural=structural,
        passed=_passes(lambda m: _check_highlights(m, end), HighlightList(highlights=highlights)),
    )]


STEPS = {
    "speakers": run_speakers,
    "summary": run_summary,
    "chapters": run_chapters,
    "chapter_detail": run_chapter_detail,
    "highlights": run_highlights,
}
