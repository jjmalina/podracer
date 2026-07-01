"""Liveness probe: GET /health, a plain JSON endpoint for external monitors.

Liveness is keyed off worker_heartbeat, which the worker advances at every point
the loop makes progress (each iteration, each feed, before each job). If that
timestamp is missing or older than health_heartbeat_max_age_seconds we report
unhealthy so a probe (uptime monitor, healthcheck) can alert even when systemd
still thinks the process is up — the silent-hang failure mode this exists to
catch.

Heartbeat, not last_sync: last_sync only advances once per feed-sync, so a
healthy worker grinding through a long queue drain would look stale for the whole
drain. last_sync is still reported, but purely as informational context.
"""
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from podracer.db import get_worker_heartbeat, get_worker_last_sync
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
    heartbeat = get_worker_heartbeat(db)
    last_sync = get_worker_last_sync(db)
    body = {"status": "ok", "heartbeat": heartbeat, "last_sync": last_sync}

    if heartbeat is None:
        # Worker has never pinged — not yet started, or died before its first
        # progress step.
        return JSONResponse({**body, "status": "unknown"}, status_code=503)

    # Stale if no progress within the configured window. The threshold is tied to
    # the worst-case single-job time (see Config.health_heartbeat_max_age_seconds),
    # not sync_interval, so a long-but-healthy job doesn't trip it.
    cfg = request.app.state.cfg
    max_age = cfg.health_heartbeat_max_age_seconds
    parsed = _parse_iso(heartbeat)
    age = (datetime.now(UTC) - parsed).total_seconds() if parsed else None
    if age is None or age > max_age:
        return JSONResponse({**body, "status": "stale"}, status_code=503)

    return JSONResponse(body, status_code=200)
