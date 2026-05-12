from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from podracer.feed import fetch_episodes, fetch_feed_metadata
from podracer.search import search_podcasts

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
    return request.app.state.templates.TemplateResponse("search/form.html", {
        "request": request,
    })


@router.get("/results")
def search_results(request: Request, q: str = ""):
    if not q.strip():
        return HTMLResponse("")
    results = search_podcasts(q)
    return request.app.state.templates.TemplateResponse("search/_results.html", {
        "request": request,
        "results": results,
    })


@router.get("/browse")
def browse_feed(request: Request, feed_url: str):
    meta = fetch_feed_metadata(feed_url)
    episodes = fetch_episodes(feed_url)
    return request.app.state.templates.TemplateResponse("search/browse.html", {
        "request": request,
        "meta": meta,
        "episodes": episodes,
        "format_duration": _format_duration,
    })
