import math
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from podracer.db import count_recent_episodes, get_recent_episodes
from podracer.web.deps import get_db

router = APIRouter()

PAGE_SIZE = 30

# Status filter chips; the template appends 'all'. Default is the first —
# summarized, i.e. episodes that are actually ready to read. The feed is always
# scoped to subscribed shows.
STATUSES = ["summarized", "pending"]


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def relative_time(ts: str | None) -> str:
    """Human 'time ago' from a stored timestamp.

    published_at is ISO-8601 ('2026-06-15T14:30:00'); created_at is SQLite's
    datetime('now') ('2026-06-15 14:30:00', UTC). Both are naive, and
    fromisoformat parses either separator. We treat both as UTC — for a
    personal feed the small error on feed-local published times is immaterial.
    Falls back to the date prefix if the value doesn't parse.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    now = datetime.now(UTC).replace(tzinfo=None)
    delta = (now - dt).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    days = int(delta // 86400)
    if days == 1:
        return "yesterday"
    if days <= 3:
        return f"{days}d ago"
    # More than a few days ago: show the date (drop the year when it's this
    # year). Build the day with dt.day rather than strftime("%-d") — the latter
    # is a glibc-only directive that raises ValueError on non-glibc platforms.
    if dt.year == now.year:
        return f"{dt:%b} {dt.day}"
    return f"{dt:%b} {dt.day}, {dt.year}"


@router.get("/")
def feed(
    request: Request, page: int = 1, status: str = "summarized",
    db: sqlite3.Connection = Depends(get_db),
):
    total = count_recent_episodes(db, subscribed_only=True, status=status)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages))  # clamp so OFFSET can't overshoot the data
    items = get_recent_episodes(
        db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
        subscribed_only=True, status=status,
    )
    return request.app.state.templates.TemplateResponse(request, "feed/list.html", {
        "request": request,
        "items": items,
        "page": page,
        "pages": pages,
        "total": total,
        "status": status,
        "statuses": STATUSES,
        "relative_time": relative_time,
        "format_duration": _format_duration,
    })
