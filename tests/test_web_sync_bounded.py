"""The web Sync buttons must fetch a bounded number of episodes.

On 2026-09-17 ``POST /podcasts/1/sync`` fetched the whole Macro Voices feed
(no limit), inserted 274 back-catalog episodes, and the auto-enqueue queued all
of them. The UI must never be a backfill; only the CLI's explicit --limit is.
"""
import sqlite3

from fastapi.testclient import TestClient

import podracer.web.routes.podcasts as podcasts_routes
import podracer.worker as worker_mod
from podracer.config import Config
from podracer.db import get_connection, init_db, subscribe, upsert_podcast
from podracer.process import SYNC_EPISODE_LIMIT
from podracer.web.app import create_app


def _client_with_podcasts(db_path: str, n: int) -> TestClient:
    conn = get_connection(db_path)
    init_db(conn)
    for i in range(n):
        pid = upsert_podcast(conn, f"P{i}", None, f"https://e/{i}.xml")
        subscribe(conn, pid)
    conn.commit()
    conn.close()
    return TestClient(create_app(Config(db_path=db_path)))


def test_podcast_sync_passes_the_shared_cap(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_sync(conn: sqlite3.Connection, podcast_id: int, feed_url: str, limit=None) -> int:
        calls.append({"podcast_id": podcast_id, "feed_url": feed_url, "limit": limit})
        return 0

    monkeypatch.setattr(podcasts_routes, "sync_podcast", fake_sync)
    with _client_with_podcasts(str(tmp_path / "w.db"), 1) as client:
        resp = client.post("/podcasts/1/sync", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [{"podcast_id": 1, "feed_url": "https://e/0.xml", "limit": SYNC_EPISODE_LIMIT}]


def test_sync_all_passes_the_shared_cap_to_every_podcast(tmp_path, monkeypatch):
    limits: list[int | None] = []

    def fake_sync(conn: sqlite3.Connection, podcast_id: int, feed_url: str, limit=None) -> int:
        limits.append(limit)
        return 0

    monkeypatch.setattr(podcasts_routes, "sync_podcast", fake_sync)
    with _client_with_podcasts(str(tmp_path / "w.db"), 3) as client:
        resp = client.post("/podcasts/sync-all", follow_redirects=False)

    assert resp.status_code == 303
    assert limits == [SYNC_EPISODE_LIMIT] * 3


def test_worker_and_web_share_one_cap():
    """Guard against the two drifting apart again (worker had a magic 10)."""
    assert worker_mod.SYNC_EPISODE_LIMIT is SYNC_EPISODE_LIMIT
    assert podcasts_routes.SYNC_EPISODE_LIMIT is SYNC_EPISODE_LIMIT
