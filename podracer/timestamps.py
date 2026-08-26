"""Strict HH:MM:SS timestamp handling and timeline geometry shared across the
pipeline.

Transcript lines, chapters, and highlights all carry positions as HH:MM:SS
strings — the storage and LLM interchange format. Anything that compares,
sorts, bins, or bounds them must go through parsed seconds: string comparison
breaks across format variants (unpadded hours, the four-field misfires the
chapters pass has emitted in prod). This module owns that vocabulary; the LLM
pipeline (summarize.py) and the web layer both import it from here.
"""
import re
from collections.abc import Iterator, Sequence
from typing import Protocol

_HMS_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")

# Chapter/highlight stamps may run this far past the last transcript line
# (models round up; transcripts end mid-sentence). Beyond it, a stamp does not
# belong to this episode's timeline.
TS_SLACK_SECONDS = 60

# A timeline shorter than this isn't worth chaptering: sub-minute audio is a
# trailer or a broken transcription, and running the timeline passes on it
# either produces noise or (for near-zero extents) trips the past-end guard on
# every item. Such episodes get a prose-only summary.
MIN_TIMELINE_SECONDS = 60

# Transcript line prefix as written by transcribe.py / whisper_service.
TS_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def parse_timestamp(ts: str) -> int | None:
    """Seconds for a strict HH:MM:SS string, else None.

    The LLM passes are prompted for HH:MM:SS but occasionally emit something
    else (observed in prod: four-field "01:01:56:00" strings, and "MM:SS:00"
    with the position shifted into the wrong fields). Callers treat None as
    malformed rather than guessing at the intended position.

    Deliberately strict — for *position* stamps, where a two-field "04:15" is
    ambiguous (MM:SS or HH:MM?). For RSS <itunes:duration> values, which are
    legitimately flexible, use the lenient ``podracer.feed.parse_duration``."""
    m = _HMS_RE.match(ts.strip())
    if m is None:
        return None
    h, mm, ss = (int(g) for g in m.groups())
    return h * 3600 + mm * 60 + ss


def format_timestamp(seconds: float) -> str:
    """Canonical zero-padded HH:MM:SS for a second count."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def chronological_key(seconds: int | None, timestamp: str) -> tuple[int, float, str]:
    """Sort key putting parsed items in time order first and unparseable ones
    last, ordered by their raw string (which preserves the old lexicographic
    order among uniformly-formatted legacy timestamps)."""
    if seconds is None:
        return (1, 0.0, timestamp)
    return (0, float(seconds), "")


class Timestamped(Protocol):
    """Anything carrying an HH:MM:SS stamp plus its parsed form (Chapter,
    Highlight). ``seconds`` is None for a malformed stamp."""
    timestamp: str

    @property
    def seconds(self) -> int | None: ...


def past_transcript_end(seconds: int, transcript_end: int | None) -> bool:
    """Whether a position lies beyond the last transcript line (plus slack).

    This is what catches the observed "MM:SS:00" misfire: it parses as valid
    HH:MM:SS but lands hours past the episode's end — only the transcript's
    own extent exposes it. A ``transcript_end`` of None means no usable bound;
    nothing is rejected then."""
    return transcript_end is not None and seconds > transcript_end + TS_SLACK_SECONDS


def timestamped_lines(text: str) -> Iterator[tuple[int, str]]:
    """(seconds, line) for each transcript line with a parseable stamp."""
    for line in text.splitlines():
        m = TS_LINE_RE.match(line)
        if not m:
            continue
        secs = parse_timestamp(m.group(1))
        if secs is not None:
            yield secs, line


def transcript_end_seconds(text: str) -> int | None:
    """Timestamp of the last transcript line, in seconds; None when there is
    no usable bound — including an end of 0, which is what a degenerate
    transcript produces (e.g. the Deepgram no-utterances fallback emits a
    single [00:00:00] line) and would otherwise reject every chapter and
    highlight past the first minute."""
    end = max((secs for secs, _ in timestamped_lines(text)), default=None)
    return end or None


def usable_timeline_end(text: str) -> int | None:
    """The transcript's end when it spans a usable timeline
    (>= MIN_TIMELINE_SECONDS), else None.

    None covers the Deepgram no-utterances fallback (one [00:00:00] line),
    alignment failures (many lines all stamped at or near zero — a tiny extent
    would make the past-end guard reject everything real), and sub-minute
    audio that has nothing to chapter. Callers produce a prose-only summary
    for these instead of running the timeline passes — never fail the job
    over it: re-transcribing reproduces the same transcript, so failing
    wedges the episode forever."""
    end = transcript_end_seconds(text)
    return end if end is not None and end >= MIN_TIMELINE_SECONDS else None


def transcript_is_degenerate(text: str) -> bool:
    """True when the transcript has no usable timeline — see
    :func:`usable_timeline_end`."""
    return usable_timeline_end(text) is None


def chapter_window(chapters: Sequence[Timestamped], i: int) -> tuple[int | None, int | None]:
    """[start, end) in seconds for ``chapters[i]``: start is its own stamp,
    end the next chapter stamp (in index order) that parses — a malformed
    neighbor must not widen the window to the end of the episode. (None, None)
    for a malformed start; an end of None means "to the end of the episode".

    Twin chapters sharing a start give the earlier one an empty [s, s) window
    and the later one the real span — matching the pre-validation index
    windows, where the second twin owned the content. For an all-parseable
    list this reduces exactly to consecutive index windows
    (starts[i], starts[i+1]); a test pins that equivalence."""
    start = chapters[i].seconds
    if start is None:
        return None, None
    end = next((c.seconds for c in chapters[i + 1:] if c.seconds is not None), None)
    return start, end


def split_timed[T: Timestamped](
    items: Sequence[T], transcript_end: int | None = None,
) -> tuple[list[tuple[int, T]], list[T]]:
    """(timed, orphaned): timed is (seconds, item) pairs for stamps that parse
    and — when a transcript end is known — sit within the episode (plus
    slack); orphaned items cannot be placed on the timeline, whether
    unparseable or implausibly far past the episode (the shifted legacy
    misfires). The single place that owns that convention; the pairs let
    consumers compare without re-checking."""
    timed: list[tuple[int, T]] = []
    orphaned: list[T] = []
    for item in items:
        s = item.seconds
        if s is None or past_transcript_end(s, transcript_end):
            orphaned.append(item)
        else:
            timed.append((s, item))
    return timed, orphaned
