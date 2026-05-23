"""delete_summary: powers the per-episode 'Re-summarize' button."""
from podracer.db import (
    delete_summary,
    get_episode,
    get_summary,
    save_summary,
    upsert_episode,
    upsert_podcast,
)
from tests.conftest import feed_ep


def _seed_with_summary(conn) -> int:
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    eid = conn.execute("SELECT id FROM episodes LIMIT 1").fetchone()["id"]
    save_summary(conn, eid, '{"summary": "x"}', "m", "b")
    return eid


def test_delete_summary_removes_row(conn):
    eid = _seed_with_summary(conn)
    assert get_summary(conn, eid) is not None

    assert delete_summary(conn, eid) is True
    assert get_summary(conn, eid) is None


def test_delete_summary_reverts_episode_status_to_transcribed(conn):
    eid = _seed_with_summary(conn)
    ep = get_episode(conn, eid)
    assert ep is not None and ep.status == "summarized"

    delete_summary(conn, eid)
    ep = get_episode(conn, eid)
    assert ep is not None and ep.status == "transcribed"


def test_delete_summary_no_summary_returns_false(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    eid = conn.execute("SELECT id FROM episodes LIMIT 1").fetchone()["id"]

    assert delete_summary(conn, eid) is False


def test_delete_summary_does_not_change_non_summarized_status(conn):
    """If the episode somehow has a non-summarized status, leave it alone."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    eid = conn.execute("SELECT id FROM episodes LIMIT 1").fetchone()["id"]
    save_summary(conn, eid, '{"summary": "x"}', "m", "b")
    conn.execute("UPDATE episodes SET status = 'pending' WHERE id = ?", (eid,))
    conn.commit()

    delete_summary(conn, eid)
    ep = get_episode(conn, eid)
    assert ep is not None and ep.status == "pending"
