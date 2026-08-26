"""The re-summarize flow: a forced summarize job regenerates in place (the
old summary stays readable until overwritten), plus the delete_summary db
primitive."""
from podracer.db import (
    delete_summary,
    enqueue_episode_pipeline,
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


def test_resummarize_enqueue_keeps_summary_and_forces_job(conn):
    # The route no longer deletes the summary before enqueueing — deleting up
    # front meant a job that failed every attempt permanently destroyed a
    # healthy summary (and the page was blank while the job ran). The
    # summarize job carries force=1 instead.
    eid = _seed_with_summary(conn)

    result = enqueue_episode_pipeline(conn, eid, force_summarize=True)

    assert result is not None
    assert get_summary(conn, eid) is not None  # still readable until overwritten
    rows = conn.execute("SELECT kind, force FROM jobs ORDER BY id").fetchall()
    assert [(r["kind"], r["force"]) for r in rows] == [("transcribe", 0), ("summarize", 1)]
