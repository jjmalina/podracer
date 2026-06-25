"""HTML routes for the digest feed + detail, plus the regenerate admin action."""
import math
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from podracer.db import count_digests, get_digest, get_digests
from podracer.digest import (
    DigestData,
    day_period,
    format_period_label,
    generate_and_save,
    week_period,
)
from podracer.models import DigestRecord
from podracer.web.deps import get_db
from podracer.web.routes.feed import relative_time

router = APIRouter()

PAGE_SIZE = 20

# The feed's kind toggle. 'day' first = the default view.
KINDS = ["day", "week"]


def _period_from(kind: str, period_start: str):
    """Rebuild the canonical Period from a stored start date, so the [start, end)
    window is always recomputed (not trusted from the row)."""
    start = date.fromisoformat(period_start)
    return week_period(start) if kind == "week" else day_period(start)


def _card(rec: DigestRecord) -> dict:
    """A lightweight feed-card view of a stored digest (no full tree)."""
    start = date.fromisoformat(rec.period_start)
    end = date.fromisoformat(rec.period_end)
    try:
        data = DigestData.model_validate_json(rec.data)
        overview = data.overview
        teaser = [{"topic": t.topic, "count": t.episode_count} for t in data.topics[:2]]
        topic_count = len(data.topics)
    except Exception:
        overview, teaser, topic_count = "", [], 0
    return {
        "kind": rec.kind,
        "period_start": rec.period_start,
        "label": format_period_label(rec.kind, start, end),
        "episode_count": rec.episode_count,
        "overview": overview,
        "teaser": teaser,
        "topic_count": topic_count,
        "created_at": rec.created_at,
    }


@router.get("/digests")
def digests_feed(
    request: Request, page: int = 1, kind: str = "day",
    db: sqlite3.Connection = Depends(get_db),
):
    if kind not in KINDS:
        kind = "day"
    total = count_digests(db, kind=kind)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages))
    rows = get_digests(db, kind=kind, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return request.app.state.templates.TemplateResponse(request, "digests/list.html", {
        "request": request,
        "cards": [_card(r) for r in rows],
        "kind": kind,
        "kinds": KINDS,
        "page": page,
        "pages": pages,
        "total": total,
        "relative_time": relative_time,
    })


@router.get("/digests/{kind}/{period_start}")
def digest_detail(
    request: Request, kind: str, period_start: str,
    db: sqlite3.Connection = Depends(get_db),
):
    rec = get_digest(db, kind, period_start)
    if not rec:
        return request.app.state.templates.TemplateResponse(request, "base.html", {
            "request": request,
        }, status_code=404)

    period = _period_from(rec.kind, rec.period_start)
    data: DigestData | None = None
    try:
        data = DigestData.model_validate_json(rec.data)
    except Exception:
        pass

    return request.app.state.templates.TemplateResponse(request, "digests/detail.html", {
        "request": request,
        "rec": rec,
        "data": data,
        "label": format_period_label(period.kind, period.start, period.end),
        "relative_time": relative_time,
        "flash": request.query_params.get("flash"),
    })


@router.post("/digests/{kind}/{period_start}/regenerate")
def digest_regenerate(
    request: Request, kind: str, period_start: str,
    db: sqlite3.Connection = Depends(get_db),
):
    cfg = request.app.state.cfg
    period = _period_from(kind, period_start)
    try:
        generate_and_save(db, cfg, period)
        flash = "regenerated"
    except Exception:
        flash = "regenerate-failed"
    return RedirectResponse(url=f"/digests/{kind}/{period_start}?flash={flash}", status_code=303)
