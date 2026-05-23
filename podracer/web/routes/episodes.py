from typing import TypedDict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from podracer.db import (
    enqueue_episode_pipeline,
    get_episode,
    get_podcast,
    get_summary,
    get_transcript,
)
from podracer.summarize import Chapter, Insight, PodcastSummary, SpeakerIdentification, SpeakerTake


class ChapterBucket(TypedDict):
    insights: list[Insight]
    takes: list[SpeakerTake]


class ChapterEntry(ChapterBucket):
    chapter: Chapter


router = APIRouter()

AD_KEYWORDS = {"advertisement", "ad ", "ad)", "sponsor", "commercial", "promo", "voiceover", "disclosure"}

# Sorts after any well-formed HH:MM:SS string, so an item past the last
# chapter still falls inside the final window.
_END_SENTINEL = "99:99:99"


def _is_ad_speaker(s: SpeakerIdentification) -> bool:
    role = s.role.lower()
    name = s.name.lower()
    return any(kw in role or kw in name for kw in AD_KEYWORDS)


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _empty_bucket() -> ChapterBucket:
    return {"insights": [], "takes": []}


def _nest_under_chapters(
    summary: PodcastSummary,
) -> tuple[list[ChapterEntry] | None, ChapterBucket, ChapterBucket]:
    """Bin insights and speaker takes into chapter windows.

    Returns (chapters_nested, pre_chapter, orphan). chapters_nested is
    None when the summary has no chapters — the caller should fall back
    to the flat render.
    """
    chapters = summary.chapters
    if not chapters:
        return None, _empty_bucket(), _empty_bucket()

    first_ts = chapters[0].timestamp
    pre_chapter: ChapterBucket = {
        "insights": [x for x in summary.insights if x.timestamp < first_ts],
        "takes": [x for x in summary.speaker_takes if x.timestamp < first_ts],
    }

    nested: list[ChapterEntry] = []
    placed_insights: set[int] = set()
    placed_takes: set[int] = set()
    for i, ch in enumerate(chapters):
        start = ch.timestamp
        end = chapters[i + 1].timestamp if i + 1 < len(chapters) else _END_SENTINEL
        ch_insights = [x for x in summary.insights if start <= x.timestamp < end]
        ch_takes = [x for x in summary.speaker_takes if start <= x.timestamp < end]
        placed_insights.update(id(x) for x in ch_insights)
        placed_takes.update(id(x) for x in ch_takes)
        nested.append({"chapter": ch, "insights": ch_insights, "takes": ch_takes})

    orphan: ChapterBucket = {
        "insights": [
            x for x in summary.insights
            if x.timestamp >= first_ts and id(x) not in placed_insights
        ],
        "takes": [
            x for x in summary.speaker_takes
            if x.timestamp >= first_ts and id(x) not in placed_takes
        ],
    }

    return nested, pre_chapter, orphan


@router.get("/episodes/{episode_id}")
def episode_detail(request: Request, episode_id: int):
    db = request.app.state.db
    episode = get_episode(db, episode_id)
    if not episode:
        return request.app.state.templates.TemplateResponse(request, "base.html", {
            "request": request,
        }, status_code=404)

    podcast = get_podcast(db, episode.podcast_id)

    summary = None
    chapters_nested: list[ChapterEntry] | None = None
    pre_chapter: ChapterBucket = _empty_bucket()
    orphan: ChapterBucket = _empty_bucket()
    record = get_summary(db, episode_id)
    if record:
        try:
            summary = PodcastSummary.model_validate_json(record.data)
            summary.speakers = [s for s in summary.speakers if not _is_ad_speaker(s)]
            summary.insights.sort(key=lambda i: i.timestamp)
            summary.speaker_takes.sort(key=lambda t: t.timestamp)
            chapters_nested, pre_chapter, orphan = _nest_under_chapters(summary)
        except Exception:
            pass

    has_transcript = get_transcript(db, episode_id) is not None
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
        "chapters_nested": chapters_nested,
        "pre_chapter": pre_chapter,
        "orphan": orphan,
        "has_transcript": has_transcript,
        "active_job": dict(active_job) if active_job else None,
        "flash": request.query_params.get("flash"),
        "format_duration": _format_duration,
    })


@router.post("/episodes/{episode_id}/enqueue")
def enqueue_episode(request: Request, episode_id: int):
    db = request.app.state.db
    cfg = request.app.state.cfg
    episode = get_episode(db, episode_id)
    if not episode:
        return RedirectResponse(url="/", status_code=303)

    result = enqueue_episode_pipeline(db, episode_id, max_attempts=cfg.max_attempts)
    flash = "enqueued" if result else "already-queued"
    return RedirectResponse(url=f"/episodes/{episode_id}?flash={flash}", status_code=303)
