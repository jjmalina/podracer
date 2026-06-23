"""Tests for per-podcast transaction boundaries in Worker._sync_feeds.

The sync work itself lives in process.sync_podcast/apply_feed (shared by the
CLI and web paths too), so the feed fetch and episode upsert are patched on the
process module, where the worker reaches them.
"""
import pytest

import podracer.process as process_mod
from podracer.config import Config
from podracer.db import get_podcast, subscribe, upsert_episode, upsert_podcast
from podracer.models import FeedMetadata
from podracer.worker import Worker
from tests.conftest import feed_ep


def _cfg() -> Config:
    return Config(max_attempts=3)


def _make_podcast(conn, feed_url: str) -> int:
    pid = upsert_podcast(conn, f"pod {feed_url}", "author", feed_url, None, None)
    subscribe(conn, pid)
    return pid


def _episode_count(conn, podcast_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id = ?", (podcast_id,),
    ).fetchone()
    return row["n"]


@pytest.fixture
def feeds(monkeypatch):
    """Patch fetch_feed to serve canned (metadata, episodes) keyed by feed_url.

    Each value may be a plain episode list, or a (categories, episodes) tuple
    when a test cares about the topic tags applied during sync.
    """
    canned: dict[str, list | tuple] = {}

    def fake_fetch_feed(url, limit=None):
        value = canned[url]
        categories, episodes = value if isinstance(value, tuple) else ([], value)
        return FeedMetadata(title="t", feed_url=url, categories=categories), episodes

    monkeypatch.setattr(process_mod, "fetch_feed", fake_fetch_feed)
    return canned


def test_successful_sync_commits_episodes_and_watermark(conn, feeds):
    pid = _make_podcast(conn, "https://x/feed")
    feeds["https://x/feed"] = [feed_ep("g1"), feed_ep("g2")]

    Worker(conn, _cfg())._sync_feeds()

    assert _episode_count(conn, pid) == 2
    assert get_podcast(conn, pid).last_synced_at is not None


def test_midbatch_failure_rolls_back_partial_upserts(conn, feeds, monkeypatch):
    pid = _make_podcast(conn, "https://x/feed")
    feeds["https://x/feed"] = [feed_ep("g1"), feed_ep("g2"), feed_ep("g3")]

    calls = {"n": 0}

    def flaky_upsert(c, podcast_id, ep):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom on third episode")
        upsert_episode(c, podcast_id, ep)

    monkeypatch.setattr(process_mod, "upsert_episode", flaky_upsert)
    Worker(conn, _cfg())._sync_feeds()

    # The whole batch rolls back — no partial episodes, no watermark bump.
    assert _episode_count(conn, pid) == 0
    assert get_podcast(conn, pid).last_synced_at is None


def test_failed_podcast_does_not_leak_into_next_commit(conn, feeds, monkeypatch):
    pid_bad = _make_podcast(conn, "https://bad/feed")
    pid_good = _make_podcast(conn, "https://good/feed")
    feeds["https://bad/feed"] = [feed_ep("b1"), feed_ep("b2")]
    feeds["https://good/feed"] = [feed_ep("g1")]

    def flaky_upsert(c, podcast_id, ep):
        if ep.guid == "b2":
            raise RuntimeError("boom")
        upsert_episode(c, podcast_id, ep)

    monkeypatch.setattr(process_mod, "upsert_episode", flaky_upsert)
    Worker(conn, _cfg())._sync_feeds()

    # The good podcast's commit must not sweep in the bad podcast's
    # pending partial batch.
    assert _episode_count(conn, pid_bad) == 0
    assert get_podcast(conn, pid_bad).last_synced_at is None
    assert _episode_count(conn, pid_good) == 1
    assert get_podcast(conn, pid_good).last_synced_at is not None


def test_fetch_failure_skips_podcast_and_continues(conn, feeds, monkeypatch):
    pid_bad = _make_podcast(conn, "https://bad/feed")
    pid_good = _make_podcast(conn, "https://good/feed")
    feeds["https://good/feed"] = [feed_ep("g1")]

    def fetch(url, limit=None):
        if url == "https://bad/feed":
            raise RuntimeError("network down")
        return FeedMetadata(title="t", feed_url=url, categories=[]), feeds[url]

    monkeypatch.setattr(process_mod, "fetch_feed", fetch)
    Worker(conn, _cfg())._sync_feeds()

    assert _episode_count(conn, pid_bad) == 0
    assert _episode_count(conn, pid_good) == 1


def test_sync_applies_topic_tags_from_feed(conn, feeds):
    pid = _make_podcast(conn, "https://x/feed")
    feeds["https://x/feed"] = (["Business", "Investing"], [feed_ep("g1")])

    Worker(conn, _cfg())._sync_feeds()

    assert get_podcast(conn, pid).topics == ["Business", "Investing"]


def test_resync_with_no_categories_keeps_existing_tags(conn, feeds):
    """A category-less sync (transient feed hiccup) must not wipe good tags."""
    pid = _make_podcast(conn, "https://x/feed")
    feeds["https://x/feed"] = (["Technology"], [feed_ep("g1")])
    Worker(conn, _cfg())._sync_feeds()
    assert get_podcast(conn, pid).topics == ["Technology"]

    # Next sync returns no categories — tags survive, episodes still update.
    feeds["https://x/feed"] = ([], [feed_ep("g1"), feed_ep("g2")])
    Worker(conn, _cfg())._sync_feeds()
    assert get_podcast(conn, pid).topics == ["Technology"]
    assert _episode_count(conn, pid) == 2
