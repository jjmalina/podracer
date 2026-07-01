"""GET /health: 200 when the worker heartbeat is fresh, 503 when stale or absent.

Liveness is keyed off worker_heartbeat against health_heartbeat_max_age_seconds
— fresh inside that window, stale beyond it, and "unknown" (also 503) when the
worker has never pinged. last_sync is reported for context but does not drive the
verdict: a fresh heartbeat with a stale last_sync (a long queue drain) is still
healthy.
"""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from podracer.config import Config
from podracer.db import (
    get_connection,
    init_db,
    set_worker_heartbeat,
    set_worker_last_sync,
)
from podracer.web.app import create_app

# Small threshold so tests can straddle it with a few minutes.
_MAX_AGE = 600  # 10 minutes


def _client(
    db_path: str, heartbeat: str | None = None, last_sync: str | None = None,
) -> TestClient:
    conn = get_connection(db_path)
    init_db(conn)
    if heartbeat is not None:
        set_worker_heartbeat(conn, heartbeat)
    if last_sync is not None:
        set_worker_last_sync(conn, last_sync)
    conn.commit()
    conn.close()
    return TestClient(create_app(Config(
        db_path=db_path, health_heartbeat_max_age_seconds=_MAX_AGE,
    )))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_health_ok_when_heartbeat_fresh(tmp_path):
    fresh = _iso(datetime.now(UTC) - timedelta(minutes=1))
    with _client(str(tmp_path / "h.db"), heartbeat=fresh, last_sync=fresh) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["heartbeat"] == fresh
        assert body["last_sync"] == fresh


def test_health_stale_when_heartbeat_old(tmp_path):
    # 30 minutes ago, well past the 10-minute threshold.
    old = _iso(datetime.now(UTC) - timedelta(minutes=30))
    with _client(str(tmp_path / "h.db"), heartbeat=old) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "stale"
        assert body["heartbeat"] == old


def test_health_unknown_when_never_pinged(tmp_path):
    with _client(str(tmp_path / "h.db"), heartbeat=None) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unknown"
        assert body["heartbeat"] is None


def test_health_ok_when_draining_despite_stale_last_sync(tmp_path):
    # The regression this fix targets: worker is busy draining a long queue, so
    # last_sync is old, but the per-job heartbeat is fresh -> still healthy.
    fresh = _iso(datetime.now(UTC) - timedelta(minutes=1))
    old = _iso(datetime.now(UTC) - timedelta(hours=2))
    with _client(str(tmp_path / "h.db"), heartbeat=fresh, last_sync=old) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
