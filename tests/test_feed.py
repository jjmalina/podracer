"""Home feed: the cross-show recent-episode query + the `/` route.

The query is the one genuinely new DB primitive — everything else in the app
reads episodes per-podcast. Ordering must be strict newest-first with a stable
id tiebreak so pagination windows don't drop or repeat rows.
"""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from podracer.config import Config
from podracer.db import (
    count_recent_episodes,
    enqueue_episode_pipeline,
    get_connection,
    get_recent_episodes,
    init_db,
    save_summary,
    subscribe,
    upsert_episode,
    upsert_podcast,
)
from podracer.web.app import create_app
from podracer.web.routes.feed import relative_time
from tests.conftest import feed_ep, set_episode_created_at


def _set_published(conn, episode_id: int, ts: str) -> None:
    conn.execute("UPDATE episodes SET published_at = ? WHERE id = ?", (ts, episode_id))
    conn.commit()


def test_recent_episodes_newest_first(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("a", title="A"))
    upsert_episode(conn, pid, feed_ep("b", title="B"))
    upsert_episode(conn, pid, feed_ep("c", title="C"))
    _set_published(conn, 1, "2026-01-01T00:00:00")
    _set_published(conn, 2, "2026-03-01T00:00:00")
    _set_published(conn, 3, "2026-02-01T00:00:00")

    items = get_recent_episodes(conn, limit=10)
    assert [it.title for it in items] == ["B", "C", "A"]


def test_null_published_falls_back_to_created_at(conn):
    """An episode with no published_at sorts by its created_at via COALESCE."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("dated", title="Dated"))
    upsert_episode(conn, pid, feed_ep("undated", title="Undated"))
    _set_published(conn, 1, "2026-01-01T00:00:00")
    set_episode_created_at(conn, 1, "2026-01-01 00:00:00")
    set_episode_created_at(conn, 2, "2026-05-01 00:00:00")  # newer, no published_at

    items = get_recent_episodes(conn, limit=10)
    assert [it.title for it in items] == ["Undated", "Dated"]


def test_subscribed_only_filter(conn):
    sub = upsert_podcast(conn, "Sub", None, "https://e/sub.xml")
    subscribe(conn, sub)
    unsub = upsert_podcast(conn, "Unsub", None, "https://e/unsub.xml")
    upsert_episode(conn, sub, feed_ep("s1", title="FromSub"))
    upsert_episode(conn, unsub, feed_ep("u1", title="FromUnsub"))

    subbed = get_recent_episodes(conn, limit=10, subscribed_only=True)
    assert [it.title for it in subbed] == ["FromSub"]
    assert count_recent_episodes(conn, subscribed_only=True) == 1

    everything = get_recent_episodes(conn, limit=10, subscribed_only=False)
    assert {it.title for it in everything} == {"FromSub", "FromUnsub"}
    assert count_recent_episodes(conn, subscribed_only=False) == 2


def test_pagination_windows_are_disjoint_and_complete(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    for i in range(25):
        upsert_episode(conn, pid, feed_ep(f"ep{i}", title=f"E{i}"))
    # All share a timestamp so the id DESC tiebreak does the ordering.
    conn.execute("UPDATE episodes SET published_at = '2026-04-01T00:00:00'")
    conn.commit()

    page1 = get_recent_episodes(conn, limit=10, offset=0)
    page2 = get_recent_episodes(conn, limit=10, offset=10)
    page3 = get_recent_episodes(conn, limit=10, offset=20)
    assert [len(page1), len(page2), len(page3)] == [10, 10, 5]

    ids = [it.id for it in page1 + page2 + page3]
    assert len(set(ids)) == 25                 # no repeats across pages
    assert ids == sorted(ids, reverse=True)    # strict id DESC tiebreak


def test_active_job_surfaces_in_feed(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("ep", title="E"))
    enqueue_episode_pipeline(conn, 1)

    item = get_recent_episodes(conn, limit=10)[0]
    assert item.active_kind  # an in-flight job's kind, not None

    # A second show with no jobs reports no active kind.
    other = upsert_podcast(conn, "Q", None, "https://e/q.xml")
    subscribe(conn, other)
    upsert_episode(conn, other, feed_ep("idle", title="Idle"))
    idle = next(it for it in get_recent_episodes(conn, limit=10) if it.title == "Idle")
    assert idle.active_kind is None


def test_status_filter(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("done", title="Done"))
    upsert_episode(conn, pid, feed_ep("waiting", title="Waiting"))
    save_summary(conn, 1, '{"summary": "x"}', "m", "b")  # episode 1 -> summarized

    only_sum = get_recent_episodes(conn, limit=10, status="summarized")
    assert [it.title for it in only_sum] == ["Done"]
    assert count_recent_episodes(conn, status="summarized") == 1

    everything = get_recent_episodes(conn, limit=10, status="all")
    assert {it.title for it in everything} == {"Done", "Waiting"}
    # No status arg / None = no filter (same as 'all').
    assert len(get_recent_episodes(conn, limit=10)) == 2


def test_relative_time_switches_to_date_after_a_few_days():
    now = datetime.now(UTC).replace(tzinfo=None)
    assert relative_time((now - timedelta(hours=2, minutes=1)).isoformat()) == "2h ago"
    assert relative_time((now - timedelta(days=1, hours=2)).isoformat()) == "yesterday"
    assert relative_time((now - timedelta(days=2, hours=1)).isoformat()) == "2d ago"
    assert relative_time((now - timedelta(days=3, hours=1)).isoformat()) == "3d ago"
    # More than a few days ago: a real date, never "Nd ago" / "Nw ago".
    older = relative_time((now - timedelta(days=12)).isoformat())
    assert "ago" not in older
    # A prior-year date keeps the year.
    assert relative_time("2020-06-15T00:00:00") == "Jun 15, 2020"


def _seed_summarized(db_path: str) -> None:
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "My Show", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("sum", title="Hello Episode"))
    upsert_episode(conn, pid, feed_ep("pend", title="Pending One"))
    save_summary(conn, 1, '{"summary": "x"}', "m", "b")  # episode 1 -> summarized
    conn.commit()  # upsert_episode doesn't commit; the app reads a fresh connection
    conn.close()


def test_feed_route_defaults_to_summarized(tmp_path):
    db_path = str(tmp_path / "feed.db")
    _seed_summarized(db_path)
    app = create_app(Config(db_path=db_path))
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Hello Episode" in resp.text      # summarized → shown by default
        assert "My Show" in resp.text
        assert "Pending One" not in resp.text     # non-summarized hidden by default
        assert "scope=all" not in resp.text       # no subscribed/all control in the UI


def test_feed_route_status_all_shows_other_statuses(tmp_path):
    db_path = str(tmp_path / "feed.db")
    _seed_summarized(db_path)
    app = create_app(Config(db_path=db_path))
    with TestClient(app) as client:
        assert "Pending One" not in client.get("/").text         # default summarized
        assert "Pending One" in client.get("/?status=all").text   # all statuses


def test_summarized_episode_hides_stale_running_badge(tmp_path):
    """A summarized episode is terminal; a leftover queued/running job must not
    render a contradictory 'processing' badge (the episode-5578 bug)."""
    db_path = str(tmp_path / "feed.db")
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "My Show", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("done", title="Done Episode"))
    save_summary(conn, 1, '{"summary": "x"}', "m", "b")  # episode 1 -> summarized
    # An orphaned queued job the worker never drained.
    conn.execute("INSERT INTO jobs (episode_id, kind, status) VALUES (1, 'transcribe', 'queued')")
    conn.commit()
    conn.close()

    app = create_app(Config(db_path=db_path))
    with TestClient(app) as client:
        default = client.get("/").text
        assert "Done Episode" in default
        assert "badge-running" not in default           # no contradictory 'transcribe…'
        allv = client.get("/?status=all").text
        assert "badge-summarized" in allv               # shows the real terminal status
        assert "badge-running" not in allv
