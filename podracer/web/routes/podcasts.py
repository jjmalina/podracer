from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from podracer.db import (
    get_all_podcasts,
    get_episode_count,
    get_episodes,
    get_podcast,
    subscribe,
    unsubscribe,
    update_podcast_synced,
    upsert_episode,
)
from podracer.feed import fetch_episodes

router = APIRouter()


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


@router.get("/")
def index():
    return RedirectResponse(url="/podcasts")


@router.get("/podcasts")
def podcast_list(request: Request):
    db = request.app.state.db
    podcasts = get_all_podcasts(db)
    items = [{"podcast": p, "episode_count": get_episode_count(db, p.id)} for p in podcasts]
    return request.app.state.templates.TemplateResponse("podcasts/list.html", {
        "request": request,
        "podcasts": items,
    })


@router.post("/podcasts/{podcast_id}/subscribe")
def podcast_subscribe(request: Request, podcast_id: int):
    db = request.app.state.db
    subscribe(db, podcast_id)
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/{podcast_id}/unsubscribe")
def podcast_unsubscribe(request: Request, podcast_id: int):
    db = request.app.state.db
    unsubscribe(db, podcast_id)
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


def _sync_feed(db, podcast_id: int, feed_url: str) -> int:
    episodes = fetch_episodes(feed_url)
    for ep in episodes:
        upsert_episode(db, podcast_id, ep)
    db.commit()
    update_podcast_synced(db, podcast_id)
    return len(episodes)


@router.post("/podcasts/{podcast_id}/sync")
def podcast_sync(request: Request, podcast_id: int):
    db = request.app.state.db
    podcast = get_podcast(db, podcast_id)
    if podcast:
        _sync_feed(db, podcast_id, podcast.feed_url)
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/sync-all")
def podcast_sync_all(request: Request):
    db = request.app.state.db
    for podcast in get_all_podcasts(db):
        _sync_feed(db, podcast.id, podcast.feed_url)
    return RedirectResponse(url="/podcasts", status_code=303)


STATUSES = ["summarized", "transcribed", "downloaded", "pending"]


@router.get("/podcasts/{podcast_id}")
def podcast_detail(request: Request, podcast_id: int, status: str = "summarized"):
    db = request.app.state.db
    podcast = get_podcast(db, podcast_id)
    if not podcast:
        return request.app.state.templates.TemplateResponse("base.html", {
            "request": request,
        }, status_code=404)
    episodes = get_episodes(db, podcast_id)
    if status != "all":
        episodes = [ep for ep in episodes if ep.status == status]
    return request.app.state.templates.TemplateResponse("podcasts/detail.html", {
        "request": request,
        "podcast": podcast,
        "episodes": episodes,
        "format_duration": _format_duration,
        "current_status": status,
        "statuses": STATUSES,
    })
