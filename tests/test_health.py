"""GET /health: 200 when worker_last_sync is fresh, 503 when stale or absent.

Staleness is 2x sync_interval_minutes — fresh inside that window, stale beyond
it, and "unknown" (also 503) when the worker has never recorded a sync.
"""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from podracer.config import Config
from podracer.db import get_connection, init_db, set_worker_last_sync
from podracer.web.app import create_app


def _client(db_path: str, last_sync: str | None = None) -> TestClient:
    conn = get_connection(db_path)
    init_db(conn)
    if last_sync is not None:
        set_worker_last_sync(conn, last_sync)
    conn.commit()
    conn.close()
    # 5-minute interval -> a 10-minute staleness threshold.
    return TestClient(create_app(Config(db_path=db_path, sync_interval_minutes=5)))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_health_ok_when_last_sync_fresh(tmp_path):
    fresh = _iso(datetime.now(UTC) - timedelta(minutes=1))
    with _client(str(tmp_path / "h.db"), last_sync=fresh) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["last_sync"] == fresh


def test_health_stale_when_last_sync_old(tmp_path):
    # 30 minutes ago, well past the 10-minute (2 x 5) threshold.
    old = _iso(datetime.now(UTC) - timedelta(minutes=30))
    with _client(str(tmp_path / "h.db"), last_sync=old) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "stale"
        assert body["last_sync"] == old


def test_health_unknown_when_never_synced(tmp_path):
    with _client(str(tmp_path / "h.db"), last_sync=None) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unknown"
        assert body["last_sync"] is None
