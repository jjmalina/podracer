"""Queue mechanics: claim, dependency ordering, retry, cascade-block,
orphan recovery."""
from podracer.db import (
    cascade_block_dependents,
    claim_next_job,
    enqueue_episode_pipeline,
    get_job_counts,
    mark_job_done,
    mark_job_failed,
    reset_running_jobs,
    upsert_episode,
    upsert_podcast,
)
from tests.conftest import feed_ep


def _seed(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    return pid


def test_claim_returns_none_when_queue_empty(conn):
    assert claim_next_job(conn) is None


def test_claim_marks_job_running(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1)

    job = claim_next_job(conn)
    assert job is not None
    assert job.status == "running"
    assert job.started_at is not None


def test_summarize_blocked_until_transcribe_done(conn):
    """The dependency clause in claim_next_job: a summarize job only becomes
    claimable once its parent transcribe job is 'done'."""
    _seed(conn)
    transcribe_id, summarize_id = enqueue_episode_pipeline(conn, 1)  # type: ignore[misc]

    first = claim_next_job(conn)
    assert first is not None
    assert first.kind == "transcribe"
    assert first.id == transcribe_id

    # While transcribe is running, summarize is invisible.
    second = claim_next_job(conn)
    assert second is None

    mark_job_done(conn, first.id)
    third = claim_next_job(conn)
    assert third is not None
    assert third.kind == "summarize"
    assert third.id == summarize_id


def test_mark_job_failed_retries_until_max_attempts(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1, max_attempts=3)

    job = claim_next_job(conn)
    assert job is not None

    # First failure → requeue, not terminal
    terminal = mark_job_failed(conn, job.id, "boom 1")
    assert terminal is False
    row = conn.execute("SELECT status, attempts FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 1

    # Second failure → still queued
    job = claim_next_job(conn)
    assert job is not None
    terminal = mark_job_failed(conn, job.id, "boom 2")
    assert terminal is False

    # Third failure → terminal
    job = claim_next_job(conn)
    assert job is not None
    terminal = mark_job_failed(conn, job.id, "boom 3")
    assert terminal is True
    row = conn.execute("SELECT status, attempts FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 3


def test_cascade_block_dependents(conn):
    """A failed transcribe blocks its dependent summarize."""
    _seed(conn)
    transcribe_id, summarize_id = enqueue_episode_pipeline(conn, 1)  # type: ignore[misc]

    # Manually mark transcribe failed (terminal), then cascade.
    conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (transcribe_id,))
    conn.commit()
    blocked = cascade_block_dependents(conn, transcribe_id)
    assert blocked == 1

    row = conn.execute("SELECT status, last_error FROM jobs WHERE id=?", (summarize_id,)).fetchone()
    assert row["status"] == "blocked"
    assert "upstream" in (row["last_error"] or "")


def test_reset_running_jobs_orphan_recovery(conn):
    """On worker startup, any job stuck in 'running' (from a previous crash)
    should be requeued."""
    _seed(conn)
    enqueue_episode_pipeline(conn, 1)
    job = claim_next_job(conn)
    assert job is not None
    # Simulate a worker crash — job stays 'running' forever otherwise.

    requeued = reset_running_jobs(conn)
    assert requeued == 1

    row = conn.execute("SELECT status, last_error, started_at FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert row["status"] == "queued"
    assert "restarted" in (row["last_error"] or "")
    assert row["started_at"] is None


def test_job_counts(conn):
    _seed(conn)
    enqueue_episode_pipeline(conn, 1)
    assert get_job_counts(conn) == {
        "queued": 2, "running": 0, "done": 0, "failed": 0, "blocked": 0,
    }

    job = claim_next_job(conn)
    assert job is not None
    counts = get_job_counts(conn)
    assert counts["running"] == 1
    assert counts["queued"] == 1

    mark_job_done(conn, job.id)
    counts = get_job_counts(conn)
    assert counts["done"] == 1


def test_fifo_ordering_among_claimable_jobs(conn):
    """When multiple jobs are claimable, the oldest by created_at wins."""
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    upsert_episode(conn, pid, feed_ep("ep2"))

    first_t, _ = enqueue_episode_pipeline(conn, 1)  # type: ignore[misc]
    enqueue_episode_pipeline(conn, 2)

    job = claim_next_job(conn)
    assert job is not None
    assert job.id == first_t
