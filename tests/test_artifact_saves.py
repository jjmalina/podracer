"""save_transcript / save_summary: RETURNING ids and paired status updates."""
from podracer.db import get_episode, save_summary, save_transcript, upsert_episode, upsert_podcast
from tests.conftest import feed_ep


def _episode(conn) -> int:
    pid = upsert_podcast(conn, "p", "a", "https://x/feed", None, None)
    upsert_episode(conn, pid, feed_ep("g1"))
    conn.commit()
    return conn.execute("SELECT id FROM episodes").fetchone()["id"]


def test_save_transcript_returns_id_and_sets_status(conn):
    eid = _episode(conn)
    tid = save_transcript(conn, eid, "text", "model-x")
    assert isinstance(tid, int)
    assert get_episode(conn, eid).status == "transcribed"


def test_save_transcript_upsert_keeps_id(conn):
    eid = _episode(conn)
    first = save_transcript(conn, eid, "text v1", "model-x")
    second = save_transcript(conn, eid, "text v2", "model-y")
    assert first == second
    row = conn.execute("SELECT text, model FROM transcripts WHERE episode_id = ?", (eid,)).fetchone()
    assert (row["text"], row["model"]) == ("text v2", "model-y")


def test_save_summary_returns_id_and_sets_status(conn):
    eid = _episode(conn)
    sid = save_summary(conn, eid, '{"summary": "x"}', "m", "b")
    assert isinstance(sid, int)
    assert get_episode(conn, eid).status == "summarized"


def test_save_summary_upsert_keeps_id(conn):
    eid = _episode(conn)
    first = save_summary(conn, eid, '{"v": 1}', "m1", "b")
    second = save_summary(conn, eid, '{"v": 2}', "m2", "b")
    assert first == second
    row = conn.execute("SELECT data, model FROM summaries WHERE episode_id = ?", (eid,)).fetchone()
    assert (row["data"], row["model"]) == ('{"v": 2}', "m2")
