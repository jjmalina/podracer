from fastapi import APIRouter, Request

from podracer.db import get_episode, get_podcast, get_summary
from podracer.summarize import PodcastSummary

router = APIRouter()


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
        except Exception:
            pass

    return request.app.state.templates.TemplateResponse("episodes/detail.html", {
        "request": request,
        "episode": episode,
        "podcast": podcast,
        "summary": summary,
        "format_duration": _format_duration,
    })
