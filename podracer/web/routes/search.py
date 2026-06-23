import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from podracer.db import (
    get_podcast,
    subscribe,
    upsert_podcast,
)
from podracer.download import ensure_artwork_cached
from podracer.feed import fetch_feed
from podracer.process import apply_feed, queue_latest_unprocessed_episode
from podracer.search import search_podcasts
from podracer.web.deps import get_db, validate_external_url

router = APIRouter(prefix="/search")


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


@router.get("")
def search_form(request: Request):
    return request.app.state.templates.TemplateResponse(request, "search/form.html", {
        "request": request,
    })


@router.get("/results")
def search_results(request: Request, q: str = ""):
    if not q.strip():
        return HTMLResponse("")
    results = search_podcasts(q)
    return request.app.state.templates.TemplateResponse(request, "search/_results.html", {
        "request": request,
        "results": results,
    })


@router.get("/browse")
def browse_feed(request: Request, feed_url: str):
    validate_external_url(feed_url)
    meta, episodes = fetch_feed(feed_url)
    return request.app.state.templates.TemplateResponse(request, "search/browse.html", {
        "request": request,
        "meta": meta,
        "episodes": episodes,
        "feed_url": feed_url,
        "format_duration": _format_duration,
    })


@router.post("/subscribe")
def subscribe_from_search(request: Request, feed_url: str, db: sqlite3.Connection = Depends(get_db)):
    validate_external_url(feed_url)
    cfg = request.app.state.cfg
    # Single parse: metadata for the podcast row, plus episodes + categories.
    meta, episodes = fetch_feed(feed_url, limit=10)
    podcast_id = upsert_podcast(db, meta.title, meta.author, feed_url,
                                meta.artwork_url, meta.description)
    subscribe(db, podcast_id)

    # Sync recent episodes (so the user has something to browse and we can queue
    # the latest) and apply topic tags from the feed's categories.
    apply_feed(db, podcast_id, meta, episodes)

    # Copy the cover now (primary trigger) so the podcast page shows it
    # immediately; the worker sync is the backstop if this fetch fails.
    podcast = get_podcast(db, podcast_id)
    if podcast:
        ensure_artwork_cached(db, podcast, cfg.media_dir)

    queue_latest_unprocessed_episode(db, cfg, podcast_id)

    return RedirectResponse(url=f"/podcasts/{podcast_id}", status_code=303)
