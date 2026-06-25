"""Liveness probe: GET /health, a plain JSON endpoint for external monitors.

The worker writes worker_last_sync every sync (every sync_interval_minutes); if
that timestamp is missing or has gone stale we report unhealthy so a probe
(uptime monitor, healthcheck) can alert even when systemd still thinks the
process is up — the silent-hang failure mode this endpoint exists to catch.
"""
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from podracer.db import get_worker_last_sync
from podracer.web.deps import get_db

router = APIRouter()


def _parse_iso(ts: str) -> datetime | None:
    """Parse a stored ISO timestamp, treating a naive value as UTC."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@router.get("/health")
def health(request: Request, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    last_sync = get_worker_last_sync(db)
    if last_sync is None:
        # Worker has never recorded a sync — not yet started, or never got far.
        return JSONResponse({"status": "unknown", "last_sync": None}, status_code=503)

    # Stale if no sync within 2x the configured interval: the worker syncs every
    # interval, so more than two intervals of silence means the loop is stuck.
    cfg = request.app.state.cfg
    max_age = 2 * cfg.sync_interval_minutes * 60
    parsed = _parse_iso(last_sync)
    age = (datetime.now(UTC) - parsed).total_seconds() if parsed else None
    if age is None or age > max_age:
        return JSONResponse({"status": "stale", "last_sync": last_sync}, status_code=503)

    return JSONResponse({"status": "ok", "last_sync": last_sync}, status_code=200)
