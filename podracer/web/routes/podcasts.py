import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from podracer import logger
from podracer.artwork import placeholder_initial, placeholder_svg
from podracer.db import (
    get_all_podcasts,
    get_episode_count,
    get_episodes,
    get_podcast,
    set_podcast_tags,
    subscribe,
    unsubscribe,
)
from podracer.download import ensure_artwork_cached
from podracer.feed import fetch_feed_metadata
from podracer.process import sync_podcast
from podracer.web.deps import get_db

router = APIRouter()


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


@router.get("/podcasts")
def podcast_list(request: Request, view: str = "grid", db: sqlite3.Connection = Depends(get_db)):
    podcasts = get_all_podcasts(db)
    items = [{"podcast": p, "episode_count": get_episode_count(db, p.id)} for p in podcasts]
    return request.app.state.templates.TemplateResponse(request, "podcasts/list.html", {
        "request": request,
        "podcasts": items,
        "view": "table" if view == "table" else "grid",
    })


@router.get("/podcasts/{podcast_id}/artwork")
def podcast_artwork(request: Request, podcast_id: int, db: sqlite3.Connection = Depends(get_db)):
    podcast = get_podcast(db, podcast_id)
    if podcast and podcast.artwork_path:
        path = Path(request.app.state.cfg.media_dir) / podcast.artwork_path
        if path.exists():
            return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    # No cached cover: a deterministic colored placeholder tile (distinct per
    # podcast). no-cache so the browser swaps in the real cover once it's cached.
    letter = placeholder_initial(podcast.title if podcast else None)
    seed = podcast.id if podcast else 0
    return Response(
        content=placeholder_svg(seed, letter),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/podcasts/{podcast_id}/subscribe")
def podcast_subscribe(request: Request, podcast_id: int, db: sqlite3.Connection = Depends(get_db)):
    subscribe(db, podcast_id)
    podcast = get_podcast(db, podcast_id)
    if podcast:
        ensure_artwork_cached(db, podcast, request.app.state.cfg.media_dir)
        # Pull topic tags from the feed if we don't already have them.
        if not podcast.topics:
            try:
                meta = fetch_feed_metadata(podcast.feed_url)
                set_podcast_tags(db, podcast_id, meta.categories)
            except Exception:
                logger.exception("subscribe_tag_fetch_failed", extra={"podcast_id": podcast_id})
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/{podcast_id}/unsubscribe")
def podcast_unsubscribe(request: Request, podcast_id: int, db: sqlite3.Connection = Depends(get_db)):
    unsubscribe(db, podcast_id)
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/{podcast_id}/sync")
def podcast_sync(request: Request, podcast_id: int, db: sqlite3.Connection = Depends(get_db)):
    podcast = get_podcast(db, podcast_id)
    if podcast:
        sync_podcast(db, podcast_id, podcast.feed_url)
    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/sync-all")
def podcast_sync_all(request: Request, db: sqlite3.Connection = Depends(get_db)):
    for podcast in get_all_podcasts(db):
        try:
            sync_podcast(db, podcast.id, podcast.feed_url)
        except Exception:
            # A single dead/slow feed (feed fetch can now raise) must not abort
            # the whole batch — drop its partial writes, log, and keep going so
            # the remaining podcasts still sync. Mirrors Worker._sync_feeds.
            db.rollback()
            logger.exception("sync_all_feed_failed", podcast=podcast.title)
    return RedirectResponse(url="/podcasts", status_code=303)


STATUSES = ["summarized", "transcribed", "downloaded", "pending"]


@router.get("/podcasts/{podcast_id}")
def podcast_detail(
    request: Request, podcast_id: int, status: str = "summarized",
    db: sqlite3.Connection = Depends(get_db),
):
    podcast = get_podcast(db, podcast_id)
    if not podcast:
        return request.app.state.templates.TemplateResponse(request, "base.html", {
            "request": request,
        }, status_code=404)
    episodes = get_episodes(db, podcast_id)
    if status != "all":
        episodes = [ep for ep in episodes if ep.status == status]
    return request.app.state.templates.TemplateResponse(request, "podcasts/detail.html", {
        "request": request,
        "podcast": podcast,
        "episodes": episodes,
        "format_duration": _format_duration,
        "current_status": status,
        "statuses": STATUSES,
    })
