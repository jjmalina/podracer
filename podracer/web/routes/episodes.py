from fastapi import APIRouter, Request

from podracer.db import get_episode, get_podcast, get_summary
from podracer.summarize import PodcastSummary, SpeakerIdentification

router = APIRouter()

AD_KEYWORDS = {"advertisement", "ad ", "ad)", "sponsor", "commercial", "promo", "voiceover", "disclosure"}


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


@router.get("/episodes/{episode_id}")
def episode_detail(request: Request, episode_id: int):
    db = request.app.state.db
    episode = get_episode(db, episode_id)
    if not episode:
        return request.app.state.templates.TemplateResponse("base.html", {
            "request": request,
        }, status_code=404)

    podcast = get_podcast(db, episode.podcast_id)

    summary = None
    record = get_summary(db, episode_id)
    if record:
        try:
            summary = PodcastSummary.model_validate_json(record.data)
            summary.speakers = [s for s in summary.speakers if not _is_ad_speaker(s)]
            summary.insights.sort(key=lambda i: i.timestamp)
            summary.speaker_takes.sort(key=lambda t: t.timestamp)
        except Exception:
            pass

    return request.app.state.templates.TemplateResponse("episodes/detail.html", {
        "request": request,
        "episode": episode,
        "podcast": podcast,
        "summary": summary,
        "format_duration": _format_duration,
    })
