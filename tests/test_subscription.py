"""Subscription + watermark business logic.

These tests cover the bug we hit in production where subscribing to a podcast
caused all 300 existing episodes to get auto-enqueued. The contract is:

  - On subscribe, podracer records a per-podcast `subscribed_at` watermark.
  - Worker only auto-enqueues episodes whose `created_at` is *after* that
    watermark.
  - Unsubscribed podcasts never enqueue.
  - Re-subscribing resets the watermark to "now".
"""
from podracer.db import (
    enqueue_episode_pipeline,
    find_new_episodes,
    subscribe,
    unsubscribe,
    upsert_episode,
    upsert_podcast,
)
from tests.conftest import (
    feed_ep,
    set_episode_created_at,
    set_podcast_subscribed_at,
)


def test_subscribe_sets_subscribed_at(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    row = conn.execute("SELECT subscribed, subscribed_at FROM podcasts WHERE id=?", (pid,)).fetchone()
    assert row["subscribed"] == 0
    assert row["subscribed_at"] is None

    subscribe(conn, pid)
    row = conn.execute("SELECT subscribed, subscribed_at FROM podcasts WHERE id=?", (pid,)).fetchone()
    assert row["subscribed"] == 1
    assert row["subscribed_at"] is not None


def test_existing_episodes_arent_enqueued_after_subscribe(conn):
    """The exact bug we hit: subscribe to a podcast that already has a backlog
    of episodes, and the worker enqueues all of them. Should now enqueue zero."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    for i in range(50):
        upsert_episode(conn, pid, feed_ep(f"ep{i}"))
    # All 50 episodes were inserted BEFORE subscribe.
    set_episode_created_at(conn, 1, "2026-05-01 00:00:00")
    # Make every episode older than the subscription timestamp.
    conn.execute("UPDATE episodes SET created_at = '2026-05-01 00:00:00'")
    conn.commit()

    subscribe(conn, pid)  # sets subscribed_at = now()
    set_podcast_subscribed_at(conn, pid, "2026-05-20 00:00:00")

    assert find_new_episodes(conn) == []


def test_new_episode_after_subscribe_gets_enqueued(conn):
    """The happy path: episode arrives via feed sync AFTER subscribing → enqueue."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    set_podcast_subscribed_at(conn, pid, "2026-05-20 00:00:00")

    upsert_episode(conn, pid, feed_ep("fresh"))
    # Mark its created_at as later than subscribed_at
    set_episode_created_at(conn, 1, "2026-05-21 00:00:00")

    new = find_new_episodes(conn)
    assert len(new) == 1
    assert new[0] == 1


def test_unsubscribed_podcast_never_enqueues(conn):
    """Even if subscribed_at is set, an unsubscribed podcast skips auto-enqueue."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    set_podcast_subscribed_at(conn, pid, "2026-05-20 00:00:00")
    upsert_episode(conn, pid, feed_ep("fresh"))
    set_episode_created_at(conn, 1, "2026-05-21 00:00:00")
    assert len(find_new_episodes(conn)) == 1  # sanity: would enqueue if subscribed

    unsubscribe(conn, pid)
    assert find_new_episodes(conn) == []


def test_resubscribe_resets_watermark(conn):
    """Resubscribing should set a fresh subscribed_at so old episodes don't
    re-enter the queue."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    set_podcast_subscribed_at(conn, pid, "2026-05-20 00:00:00")
    upsert_episode(conn, pid, feed_ep("fresh"))
    set_episode_created_at(conn, 1, "2026-05-21 00:00:00")
    assert len(find_new_episodes(conn)) == 1

    # Enqueue + drain it.
    enqueue_episode_pipeline(conn, 1)
    assert find_new_episodes(conn) == []  # active job blocks re-enqueue

    # Now unsubscribe and resubscribe.
    unsubscribe(conn, pid)
    subscribe(conn, pid)
    # The new subscribed_at is "now" — but the episode was inserted before that.
    # So it shouldn't re-appear in find_new_episodes.
    assert find_new_episodes(conn) == []


def test_active_job_blocks_re_enqueue(conn):
    """find_new_episodes must not return an episode that already has a queued
    or running job."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    set_podcast_subscribed_at(conn, pid, "2026-05-20 00:00:00")
    upsert_episode(conn, pid, feed_ep("fresh"))
    set_episode_created_at(conn, 1, "2026-05-21 00:00:00")

    assert len(find_new_episodes(conn)) == 1
    enqueue_episode_pipeline(conn, 1)
    assert find_new_episodes(conn) == []


def test_enqueue_pipeline_is_idempotent(conn):
    """Re-calling enqueue_episode_pipeline while jobs are active returns None
    (the unique index on (episode_id, kind) WHERE status IN active prevents
    duplicate inserts)."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))

    first = enqueue_episode_pipeline(conn, 1)
    assert first is not None
    assert first[0] != first[1]  # two distinct job ids

    second = enqueue_episode_pipeline(conn, 1)
    assert second is None


def test_podcast_with_no_subscribed_at_skipped(conn):
    """If subscribed_at is NULL (theoretically possible before migration ran),
    don't enqueue anything for that podcast."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    conn.execute("UPDATE podcasts SET subscribed = 1, subscribed_at = NULL WHERE id = ?", (pid,))
    conn.commit()
    upsert_episode(conn, pid, feed_ep("fresh"))
    assert find_new_episodes(conn) == []
