import sqlite3
from typing import TypedDict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from podracer.db import (
    enqueue_episode_pipeline,
    get_episode,
    get_podcast,
    get_summary,
    get_transcript,
    transcript_exists,
)
from podracer.models import Chapter, Highlight, PodcastSummary, is_ad_speaker
from podracer.timestamps import past_transcript_end, split_timed, transcript_end_seconds
from podracer.web.deps import get_db


class ChapterBucket(TypedDict):
    highlights: list[Highlight]


class ChapterEntry(ChapterBucket):
    chapter: Chapter


router = APIRouter()


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _empty_bucket() -> ChapterBucket:
    return {"highlights": []}


def _nest_under_chapters(
    summary: PodcastSummary,
    highlights: list[Highlight],
    transcript_end: int | None,
) -> tuple[list[ChapterEntry] | None, ChapterBucket, ChapterBucket]:
    """Bin ``highlights`` (the caller's already-computed effective list) into
    chapter windows — summarize.chapter_window is the one definition of those
    windows, shared with enrichment slicing.

    ``transcript_end`` comes from the episode's own transcript — the same
    anchor the write-side checks use. RSS durations are NOT trusted here:
    feeds are known to publish bare-integer minutes that get stored as
    seconds. Highlights past the transcript (plus slack) join the orphan
    bucket alongside unparseable ones (split_timed_highlights owns that
    convention).

    Returns (chapters_nested, pre_chapter, orphan). chapters_nested is None —
    the caller falls back to the un-nested render rather than mis-bin — when
    the summary has no chapters, a chapter timestamp doesn't parse (stored
    summaries predating validation), or the chapter timeline runs past the
    transcript's end: the shifted "MM:SS:00" misfire parses and sorts fine,
    and only the transcript's own extent exposes it.
    """
    chapters = summary.chapters
    if not chapters:
        return None, _empty_bucket(), _empty_bucket()
    starts = [s for c in chapters if (s := c.seconds) is not None]
    if len(starts) != len(chapters):
        return None, _empty_bucket(), _empty_bucket()
    if past_transcript_end(starts[-1], transcript_end):
        return None, _empty_bucket(), _empty_bucket()

    timed, orphaned = split_timed(highlights, transcript_end)
    pre_chapter: ChapterBucket = {
        "highlights": [h for s, h in timed if s < starts[0]],
    }

    # Consecutive index windows over the verified-parseable sorted starts —
    # exactly timestamps.chapter_window's semantics for an all-parseable list
    # (a test pins the equivalence): a twin sharing its predecessor's start
    # gets the empty [s, s) window, the later twin the real span.
    nested: list[ChapterEntry] = []
    for i, ch in enumerate(chapters):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else None
        nested.append({"chapter": ch, "highlights": [
            h for s, h in timed if start <= s and (end is None or s < end)
        ]})

    orphan: ChapterBucket = {"highlights": orphaned}

    return nested, pre_chapter, orphan


@router.get("/episodes/{episode_id}")
def episode_detail(request: Request, episode_id: int, db: sqlite3.Connection = Depends(get_db)):
    episode = get_episode(db, episode_id)
    if not episode:
        return request.app.state.templates.TemplateResponse(request, "base.html", {
            "request": request,
        }, status_code=404)

    podcast = get_podcast(db, episode.podcast_id)

    summary = None
    highlights: list[Highlight] = []
    chapters_nested: list[ChapterEntry] | None = None
    pre_chapter: ChapterBucket = _empty_bucket()
    orphan: ChapterBucket = _empty_bucket()
    record = get_summary(db, episode_id)
    if record:
        try:
            summary = PodcastSummary.model_validate_json(record.data)
            summary.speakers = [s for s in summary.speakers if not is_ad_speaker(s)]
            highlights = summary.effective_highlights()
            # The transcript text is only needed to anchor the nesting guard,
            # so load it just when there's a summary to nest.
            transcript = get_transcript(db, episode_id)
            t_end = transcript_end_seconds(transcript.text) if transcript else None
            chapters_nested, pre_chapter, orphan = _nest_under_chapters(
                summary, highlights, t_end)
        except Exception:
            pass

    has_transcript = transcript_exists(db, episode_id)
    active_job = db.execute(
        "SELECT kind, status FROM jobs WHERE episode_id = ? "
        "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
        (episode_id,),
    ).fetchone()

    return request.app.state.templates.TemplateResponse(request, "episodes/detail.html", {
        "request": request,
        "episode": episode,
        "podcast": podcast,
        "summary": summary,
        "highlights": highlights,
        "chapters_nested": chapters_nested,
        "pre_chapter": pre_chapter,
        "orphan": orphan,
        "has_transcript": has_transcript,
        "active_job": dict(active_job) if active_job else None,
        "flash": request.query_params.get("flash"),
        "format_duration": _format_duration,
    })


@router.post("/episodes/{episode_id}/enqueue")
def enqueue_episode(request: Request, episode_id: int, db: sqlite3.Connection = Depends(get_db)):
    cfg = request.app.state.cfg
    episode = get_episode(db, episode_id)
    if not episode:
        return RedirectResponse(url="/", status_code=303)

    result = enqueue_episode_pipeline(db, episode_id, max_attempts=cfg.max_attempts)
    flash = "enqueued" if result else "already-queued"
    return RedirectResponse(url=f"/episodes/{episode_id}?flash={flash}", status_code=303)


@router.post("/episodes/{episode_id}/resummarize")
def resummarize_episode(request: Request, episode_id: int, db: sqlite3.Connection = Depends(get_db)):
    cfg = request.app.state.cfg
    episode = get_episode(db, episode_id)
    if not episode:
        return RedirectResponse(url="/", status_code=303)

    active = db.execute(
        "SELECT id FROM jobs WHERE episode_id = ? "
        "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
        (episode_id,),
    ).fetchone()
    if active:
        return RedirectResponse(url=f"/episodes/{episode_id}?flash=already-queued", status_code=303)

    # The old summary stays in place until the forced job overwrites it on
    # success — deleting it up front meant a job that failed every attempt
    # permanently destroyed a perfectly good summary.
    enqueue_episode_pipeline(db, episode_id, max_attempts=cfg.max_attempts, force_summarize=True)
    return RedirectResponse(url=f"/episodes/{episode_id}?flash=resummarizing", status_code=303)
