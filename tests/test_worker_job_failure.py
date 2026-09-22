"""Worker._run_job failure path: the attempt must be persisted before the
exception is reported to Sentry.

Regression for 2026-09-18: capture_exception serialized frames holding the
whole audio file, the memory cgroup OOM-killed the worker mid-report, and the
still-"running" job was orphan-requeued on every restart — a crash loop that
never advanced past attempt 1.
"""
import pytest

import podracer.worker as worker_mod
from podracer.config import Config
from podracer.db import claim_next_job, enqueue_episode_pipeline, upsert_episode, upsert_podcast
from podracer.worker import Worker
from tests.conftest import feed_ep


def _job_row(conn, job_id: int):
    return conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,),
    ).fetchone()


@pytest.fixture
def claimed_job(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    upsert_episode(conn, pid, feed_ep("ep1"))
    enqueue_episode_pipeline(conn, 1, max_attempts=3)
    job = claim_next_job(conn)
    assert job is not None and job.kind == "transcribe"
    return job


def test_failure_is_persisted_before_sentry_capture(conn, claimed_job, monkeypatch):
    seen = {}

    def boom_dispatch(job):
        raise RuntimeError("402 payment required")

    def spy_capture(exc=None):
        # Observe DB state at the moment Sentry runs: the attempt is already
        # recorded, so a crash inside the report can't leave the job "running".
        seen["row"] = _job_row(conn, claimed_job.id)

    monkeypatch.setattr(worker_mod.sentry_sdk, "capture_exception", spy_capture)
    w = Worker(conn, Config(max_attempts=3))
    monkeypatch.setattr(w, "_dispatch", boom_dispatch)

    w._run_job(claimed_job)

    assert seen["row"]["status"] != "running"
    assert seen["row"]["attempts"] == 1
    assert "402" in seen["row"]["last_error"]


def test_crash_inside_sentry_capture_still_counts_the_attempt(conn, claimed_job, monkeypatch):
    def boom_dispatch(job):
        raise RuntimeError("boom")

    def dying_capture(exc=None):
        raise MemoryError("simulated OOM during event serialization")

    monkeypatch.setattr(worker_mod.sentry_sdk, "capture_exception", dying_capture)
    w = Worker(conn, Config(max_attempts=3))
    monkeypatch.setattr(w, "_dispatch", boom_dispatch)

    with pytest.raises(MemoryError):
        w._run_job(claimed_job)

    row = _job_row(conn, claimed_job.id)
    assert row["status"] != "running"
    assert row["attempts"] == 1
