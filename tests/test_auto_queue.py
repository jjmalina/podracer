"""queue_latest_unprocessed_episode — auto-queue on subscribe.

The contract: subscribing should give the user something to listen to within
the worker's next drain interval. Pick the most-recently-published episode
that doesn't already have a summary or an active job. Idempotent."""
from podracer.config import Config
from podracer.db import (
    enqueue_episode_pipeline,
    get_episode,
    save_summary,
    subscribe,
    upsert_episode,
    upsert_podcast,
)
from podracer.process import queue_latest_unprocessed_episode
from tests.conftest import feed_ep


def _cfg() -> Config:
    return Config(max_attempts=3)


def _ep_with_published(conn, podcast_id: int, guid: str, published_at: str) -> int:
    upsert_episode(conn, podcast_id, feed_ep(guid))
    ep_id = conn.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone()["id"]
    conn.execute("UPDATE episodes SET published_at=? WHERE id=?", (published_at, ep_id))
    conn.commit()
    return ep_id


def test_queues_most_recent_by_published_at(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    old = _ep_with_published(conn, pid, "old", "2024-01-01T00:00:00")
    mid = _ep_with_published(conn, pid, "mid", "2025-06-01T00:00:00")
    new = _ep_with_published(conn, pid, "newest", "2026-05-21T00:00:00")

    queued = queue_latest_unprocessed_episode(conn, _cfg(), pid)
    assert queued == new

    # old/mid still untouched
    for ep_id in (old, mid):
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE episode_id=?", (ep_id,),
        ).fetchone()
        assert row["n"] == 0


def test_skips_already_summarized_episode(conn):
    """If the newest episode already has a summary, fall back to the next one."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    _ep_with_published(conn, pid, "old", "2024-01-01T00:00:00")
    newest = _ep_with_published(conn, pid, "newest", "2026-05-21T00:00:00")

    # Newest already has a summary from a previous run.
    save_summary(conn, newest, '{"summary":"x","speakers":[],"chapters":[],"insights":[],"speaker_takes":[]}',
                 "deepseek/x", "openrouter")

    queued = queue_latest_unprocessed_episode(conn, _cfg(), pid)
    # Falls back to the next-most-recent (the old one).
    assert queued is not None
    assert queued != newest
    ep = get_episode(conn, queued)
    assert ep is not None
    assert ep.guid == "old"


def test_skips_episode_with_active_job(conn):
    """If the newest episode already has a queued job, fall back to next."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    mid = _ep_with_published(conn, pid, "mid", "2025-01-01T00:00:00")
    new = _ep_with_published(conn, pid, "newest", "2026-05-21T00:00:00")

    # Newest already queued (e.g., user clicked Process via UI)
    enqueue_episode_pipeline(conn, new)

    queued = queue_latest_unprocessed_episode(conn, _cfg(), pid)
    assert queued == mid


def test_no_op_when_everything_processed(conn):
    """If every episode is already summarized, return None."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    ep1 = _ep_with_published(conn, pid, "a", "2026-05-21T00:00:00")
    save_summary(conn, ep1, '{"summary":"x","speakers":[],"chapters":[],"insights":[],"speaker_takes":[]}',
                 "m", "openrouter")
    assert queue_latest_unprocessed_episode(conn, _cfg(), pid) is None


def test_no_op_when_no_episodes(conn):
    """Fresh subscribe with empty feed → None, no crash."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    assert queue_latest_unprocessed_episode(conn, _cfg(), pid) is None


def test_calling_twice_is_safe(conn):
    """Calling queue_latest twice in a row queues only once (active job blocks)."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    ep1 = _ep_with_published(conn, pid, "a", "2026-05-21T00:00:00")
    _ep_with_published(conn, pid, "b", "2025-01-01T00:00:00")

    first = queue_latest_unprocessed_episode(conn, _cfg(), pid)
    assert first == ep1

    # Second call: ep1 has active job, ep "b" is older but next in line
    second = queue_latest_unprocessed_episode(conn, _cfg(), pid)
    assert second is not None
    assert second != ep1  # picks the older one since newest is busy
