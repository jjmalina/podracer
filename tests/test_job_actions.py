"""retry_job + cancel_job: actions exposed to the UI."""
from podracer.db import (
    cancel_job,
    cascade_block_dependents,
    claim_next_job,
    enqueue_episode_pipeline,
    get_job,
    mark_job_failed,
    retry_job,
    upsert_episode,
    upsert_podcast,
)
from tests.conftest import feed_ep


def _seed(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    return pid


def _exhaust_retries(conn, job_id: int, max_attempts: int = 3) -> None:
    """Fail a job until it's terminal."""
    for _ in range(max_attempts):
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        conn.commit()
        mark_job_failed(conn, job_id, "boom")


def test_retry_resets_failed_job(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1, max_attempts=2)
    transcribe = claim_next_job(conn)
    assert transcribe is not None
    _exhaust_retries(conn, transcribe.id, max_attempts=2)
    assert get_job(conn, transcribe.id).status == "failed"  # type: ignore[union-attr]

    ok = retry_job(conn, transcribe.id)
    assert ok is True
    j = get_job(conn, transcribe.id)
    assert j is not None
    assert j.status == "queued"
    assert j.attempts == 0
    assert j.last_error is None
    assert j.started_at is None


def test_retry_unblocks_dependents(conn):
    """When a failed transcribe's summarize was cascade-blocked, retrying
    the transcribe should also unblock the summarize."""
    _seed(conn)
    transcribe_id, summarize_id = enqueue_episode_pipeline(conn, 1, max_attempts=1)  # type: ignore[misc]
    # Fail transcribe and cascade.
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (transcribe_id,))
    conn.commit()
    mark_job_failed(conn, transcribe_id, "nope")
    cascade_block_dependents(conn, transcribe_id)
    assert get_job(conn, summarize_id).status == "blocked"  # type: ignore[union-attr]

    retry_job(conn, transcribe_id)
    assert get_job(conn, transcribe_id).status == "queued"  # type: ignore[union-attr]
    assert get_job(conn, summarize_id).status == "queued"   # type: ignore[union-attr]


def test_retry_no_op_when_not_failed(conn):
    _seed(conn)
    transcribe_id, _ = enqueue_episode_pipeline(conn, 1)  # type: ignore[misc]
    # Job is queued, not failed
    assert retry_job(conn, transcribe_id) is False


def test_cancel_deletes_queued_job_and_dependents(conn):
    _seed(conn)
    transcribe_id, summarize_id = enqueue_episode_pipeline(conn, 1)  # type: ignore[misc]

    ok = cancel_job(conn, transcribe_id)
    assert ok is True
    assert get_job(conn, transcribe_id) is None
    assert get_job(conn, summarize_id) is None


def test_cancel_blocked_works(conn):
    """Cancelling a blocked job removes it."""
    _seed(conn)
    transcribe_id, summarize_id = enqueue_episode_pipeline(conn, 1, max_attempts=1)  # type: ignore[misc]
    # Fail + cascade so summarize is 'blocked'
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (transcribe_id,))
    conn.commit()
    mark_job_failed(conn, transcribe_id, "nope")
    cascade_block_dependents(conn, transcribe_id)
    assert get_job(conn, summarize_id).status == "blocked"  # type: ignore[union-attr]

    ok = cancel_job(conn, summarize_id)
    assert ok is True
    assert get_job(conn, summarize_id) is None
    # The failed transcribe stays (cancel only acts on queued/blocked).
    assert get_job(conn, transcribe_id) is not None


def test_cancel_running_is_no_op(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1)
    job = claim_next_job(conn)
    assert job is not None
    # Running jobs can't be cancelled via the UI helper.
    assert cancel_job(conn, job.id) is False
    assert get_job(conn, job.id).status == "running"  # type: ignore[union-attr]


def test_cancel_done_is_no_op(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1)
    job = claim_next_job(conn)
    assert job is not None
    conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job.id,))
    conn.commit()
    assert cancel_job(conn, job.id) is False
