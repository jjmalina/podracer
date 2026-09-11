"""Auto-enqueue must reach a fixed point.

The prod bug: find_new_episodes only excluded episodes with an *active* job, so
every episode past the subscribe watermark was re-enqueued on every sync once
its pipeline finished (or failed for good). Both jobs are idempotent no-ops when
the artifacts exist, so they ran to 'done' in a second — and left two more job
rows behind, forever. Permanently failing episodes (audio host returns 403) got
a fresh failed+blocked pair every sync too.

The contract now: an episode is auto-enqueued until its pipeline completes
(summary saved) or has failed ``auto_retry_pipelines`` times, and between
failures the worker waits out ``auto_retry_cooldown_hours`` before trying
again. max_attempts burn back-to-back within one drain, so without the
cooldown a short outage (whisper restart, LLM 5xx) would spend the whole budget
in seconds; with it, transient failures self-heal on a later sync while a
permanently broken episode converges after the budget. Past the budget only a
deliberate manual action (retry / enqueue / resummarize) touches it.

These tests drive the real Worker; the transcribe/summarize stages are faked at
the worker's import boundary so no network or model is involved.
"""
import pytest

import podracer.worker as worker_mod
from podracer.config import Config
from podracer.db import (
    enqueue_episode_pipeline,
    find_new_episodes,
    get_job_counts,
    get_summary,
    retry_job,
    save_summary,
    save_transcript,
    subscribe,
    upsert_episode,
    upsert_podcast,
)
from podracer.process import queue_latest_unprocessed_episode
from podracer.worker import Worker
from tests.conftest import feed_ep, set_episode_created_at, set_podcast_subscribed_at

SUMMARY_JSON = '{"summary":"x","speakers":[],"chapters":[],"insights":[],"speaker_takes":[]}'
SUBSCRIBED_AT = "2026-05-20 00:00:00"
AFTER_SUBSCRIBE = "2026-05-21 00:00:00"


def _cfg(
    max_attempts: int = 3, auto_retry_pipelines: int = 3,
    auto_retry_cooldown_hours: int = 6,
) -> Config:
    return Config(
        max_attempts=max_attempts,
        auto_retry_pipelines=auto_retry_pipelines,
        auto_retry_cooldown_hours=auto_retry_cooldown_hours,
    )


# Cooldown 0 = "the cooldown has always already expired": isolates the budget.
NO_COOLDOWN = 0


def _subscribed_podcast(conn) -> int:
    pid = upsert_podcast(conn, "Synthetic Show", None, "https://example.invalid/feed.xml")
    subscribe(conn, pid)
    set_podcast_subscribed_at(conn, pid, SUBSCRIBED_AT)
    return pid


def _new_episode(conn, pid: int, guid: str) -> int:
    """An episode that arrived after subscribing — eligible for auto-enqueue."""
    upsert_episode(conn, pid, feed_ep(guid))
    ep_id = conn.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone()["id"]
    set_episode_created_at(conn, ep_id, AFTER_SUBSCRIBE)
    return ep_id


def _job_count(conn, episode_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE episode_id=?", (episode_id,),
    ).fetchone()["n"]


def _statuses(conn, episode_id: int) -> list[str]:
    return sorted(
        r["status"] for r in conn.execute(
            "SELECT status FROM jobs WHERE episode_id=?", (episode_id,),
        ).fetchall()
    )


def _age_failures(conn, episode_id: int, hours: int) -> None:
    """Advance the clock: push the episode's failure timestamps ``hours`` into
    the past, the way a later sync would see them."""
    conn.execute(
        "UPDATE jobs SET finished_at = datetime(finished_at, ?) "
        "WHERE episode_id=? AND status='failed'",
        (f"-{hours} hours", episode_id),
    )
    conn.commit()


def _sync(worker: Worker, times: int = 1) -> None:
    """One worker sync cycle minus the feed fetch: discover, enqueue, drain."""
    for _ in range(times):
        worker._enqueue_new()
        worker._drain_queue()


def _discover(worker: Worker) -> list[int]:
    """What the worker's next sync would enqueue, under the worker's policy.
    (A bare find_new_episodes(conn) uses the config defaults instead.)"""
    return find_new_episodes(
        worker.conn,
        auto_retry_pipelines=worker.cfg.auto_retry_pipelines,
        auto_retry_cooldown_hours=worker.cfg.auto_retry_cooldown_hours,
    )


@pytest.fixture
def stages(monkeypatch):
    """Fake pipeline stages, patched where the worker dispatches to them.

    ``stages.fail_transcribe`` / ``stages.fail_summarize`` make that stage raise
    (a 403 from the audio host, an LLM outage); otherwise each stage saves its
    artifact the way the real one does. Calls are counted per stage.
    """
    class Stages:
        fail_transcribe: str | None = None
        fail_summarize: str | None = None
        calls = {"transcribe": 0, "summarize": 0}  # dispatches by the worker
        summary_writes = 0  # times a summary was actually (re)generated

    st = Stages()

    def fake_transcribe(conn, cfg, episode_id, *, force=False):
        st.calls["transcribe"] += 1
        if st.fail_transcribe:
            raise RuntimeError(st.fail_transcribe)
        save_transcript(conn, episode_id, "synthetic transcript", "fake:model")

    def fake_summarize(conn, cfg, episode_id, *, force=False):
        st.calls["summarize"] += 1
        if st.fail_summarize:
            raise RuntimeError(st.fail_summarize)
        if not force and get_summary(conn, episode_id):
            return None
        st.summary_writes += 1
        save_summary(conn, episode_id, SUMMARY_JSON, "fake-model", "fake")

    monkeypatch.setattr(worker_mod, "transcribe_episode", fake_transcribe)
    monkeypatch.setattr(worker_mod, "summarize_episode", fake_summarize)
    return st


# ---------- The churn ----------

def test_completed_episode_is_not_rediscovered(conn, stages):
    """The core prod bug: after the pipeline completes, every later sync
    re-enqueued a no-op transcribe+summarize pair. Now: exactly one pipeline."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-new")
    worker = Worker(conn, _cfg())

    assert find_new_episodes(conn) == [ep]
    _sync(worker)
    assert get_summary(conn, ep) is not None
    assert _job_count(conn, ep) == 2

    # 48 syncs/day on prod. None of them should add a row.
    assert find_new_episodes(conn) == []
    _sync(worker, times=10)
    assert _job_count(conn, ep) == 2
    assert stages.calls == {"transcribe": 1, "summarize": 1}
    assert get_job_counts(conn)["done"] == 2


def test_summary_alone_excludes_episode_from_discovery(conn):
    """Query-level form of the above: a summary means the pipeline is complete,
    regardless of what job history (if any) exists."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-summarized")
    assert find_new_episodes(conn) == [ep]

    save_summary(conn, ep, SUMMARY_JSON, "m", "b")
    assert find_new_episodes(conn) == []


def test_failed_transcribe_is_not_retried_within_the_cooldown(conn, stages):
    """The 403 episodes: transcribe exhausts its attempts, summarize is
    cascade-blocked. No fresh failed+blocked pair per sync — the episode waits
    out the cooldown, however many syncs happen in the meantime."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-403")
    stages.fail_transcribe = "download failed: HTTP 403"
    worker = Worker(conn, _cfg(max_attempts=3, auto_retry_cooldown_hours=6))

    _sync(worker)
    assert _statuses(conn, ep) == ["blocked", "failed"]
    assert stages.calls["transcribe"] == 3  # the attempts it was given

    assert find_new_episodes(conn) == []
    _sync(worker, times=10)
    assert _job_count(conn, ep) == 2
    assert stages.calls["transcribe"] == 3


def test_failed_summarize_is_not_retried_once_the_budget_is_spent(conn, stages):
    """Second-stage failure (transcript saved, LLM dead), cooldown already
    expired every time: the budget is what bounds it. Budget 2 means exactly
    two pipelines — two done transcribes, two failed summarizes — then none."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-llm-down")
    stages.fail_summarize = "llm unavailable"
    worker = Worker(conn, _cfg(
        max_attempts=2, auto_retry_pipelines=2, auto_retry_cooldown_hours=NO_COOLDOWN,
    ))

    _sync(worker)
    assert get_job_counts(conn) == {
        "queued": 0, "running": 0, "done": 1, "failed": 1, "blocked": 0,
    }
    assert _discover(worker) == [ep]  # one failure, budget of two

    _sync(worker, times=5)
    assert get_job_counts(conn) == {
        "queued": 0, "running": 0, "done": 2, "failed": 2, "blocked": 0,
    }
    assert stages.calls["summarize"] == 4  # 2 pipelines x 2 attempts
    assert _discover(worker) == []


def test_transient_failure_is_retried_after_the_cooldown_and_then_converges(conn, stages):
    """A whisper restart: the first pipeline burns its attempts in one drain.
    Syncing again immediately does nothing (cooldown); once the cooldown has
    passed the episode is picked up again, succeeds, and is never touched
    again. On main this self-healed on the next sync; before this commit the
    branch parked it forever."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-blip")
    stages.fail_transcribe = "whisper service unreachable"
    worker = Worker(conn, _cfg(max_attempts=3, auto_retry_cooldown_hours=6))

    _sync(worker)
    assert _statuses(conn, ep) == ["blocked", "failed"]
    stages.fail_transcribe = None  # the service is back within seconds

    # Still inside the cooldown: not our turn yet, no matter how many syncs.
    assert find_new_episodes(conn) == []
    _sync(worker, times=3)
    assert _job_count(conn, ep) == 2
    assert stages.calls["transcribe"] == 3

    # 6 hours later.
    _age_failures(conn, ep, hours=7)
    assert find_new_episodes(conn) == [ep]
    _sync(worker)
    assert get_summary(conn, ep) is not None
    assert _statuses(conn, ep) == ["blocked", "done", "done", "failed"]
    assert stages.calls == {"transcribe": 4, "summarize": 1}

    # Complete: the failed history no longer matters.
    assert find_new_episodes(conn) == []
    _age_failures(conn, ep, hours=7)
    _sync(worker, times=5)
    assert _job_count(conn, ep) == 4


def test_auto_retry_budget_bounds_a_permanently_failing_episode(conn, stages):
    """Budget 2, cooldown expired before every sync: an always-403 episode
    gets exactly two automatic pipelines across many syncs, then none. Rows
    converge at budget x 2 instead of growing every sync_interval."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-always-403")
    stages.fail_transcribe = "download failed: HTTP 403"
    worker = Worker(conn, _cfg(
        max_attempts=3, auto_retry_pipelines=2, auto_retry_cooldown_hours=NO_COOLDOWN,
    ))

    for _ in range(12):
        _sync(worker)
    assert _statuses(conn, ep) == ["blocked", "blocked", "failed", "failed"]
    assert _job_count(conn, ep) == 4
    assert stages.calls["transcribe"] == 6  # 2 pipelines x 3 attempts
    assert _discover(worker) == []

    # Aging the failures further changes nothing: the budget, not the
    # cooldown, is what holds it now.
    _age_failures(conn, ep, hours=48)
    assert _discover(worker) == []


# ---------- What must keep working ----------

def test_brand_new_episode_is_still_discovered(conn, stages):
    """A genuinely new episode arriving later is picked up — and only it."""
    pid = _subscribed_podcast(conn)
    first = _new_episode(conn, pid, "ep-1")
    worker = Worker(conn, _cfg())
    _sync(worker)
    assert _job_count(conn, first) == 2

    second = _new_episode(conn, pid, "ep-2")
    assert find_new_episodes(conn) == [second]
    _sync(worker)
    assert get_summary(conn, second) is not None
    assert _job_count(conn, first) == 2
    assert _job_count(conn, second) == 2


def test_transcript_without_summary_still_gets_summarized(conn, stages):
    """An episode transcribed outside the worker (CLI) but never summarized is
    still work to do. The existing pattern is a full pipeline: transcribe is an
    idempotent no-op on the existing transcript, summarize does the work."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-transcribed")
    save_transcript(conn, ep, "transcribed via cli", "fake:model")
    assert get_summary(conn, ep) is None

    assert find_new_episodes(conn) == [ep]
    worker = Worker(conn, _cfg())
    _sync(worker)
    assert get_summary(conn, ep) is not None
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM jobs WHERE episode_id=? ORDER BY id", (ep,),
    ).fetchall()]
    assert kinds == ["transcribe", "summarize"]

    # And it converges like everything else.
    assert find_new_episodes(conn) == []
    _sync(worker, times=3)
    assert _job_count(conn, ep) == 2


def test_manual_retry_of_failed_pipeline_still_flows(conn, stages):
    """Past the budget it's a person's call: retry_job requeues the failed
    transcribe, unblocks its summarize, and the pipeline runs to completion —
    regardless of budget or cooldown. While it's queued the episode is
    excluded as in-flight, and afterwards as complete — never re-enqueued by
    discovery in between."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-fixed-later")
    stages.fail_transcribe = "download failed: HTTP 403"
    worker = Worker(conn, _cfg(max_attempts=1, auto_retry_pipelines=1))
    _sync(worker)
    assert _discover(worker) == []  # budget spent
    failed = conn.execute(
        "SELECT id FROM jobs WHERE episode_id=? AND status='failed'", (ep,),
    ).fetchone()
    assert failed is not None

    stages.fail_transcribe = None  # the host fixed its enclosure
    assert retry_job(conn, failed["id"]) is True
    assert find_new_episodes(conn) == []  # in flight, not rediscovered
    _sync(worker)
    assert get_summary(conn, ep) is not None
    assert _job_count(conn, ep) == 2  # the same two rows, no new pipeline
    assert find_new_episodes(conn) == []


def test_manual_enqueue_and_resummarize_are_unaffected(conn, stages):
    """enqueue_episode_pipeline stays a mechanism, not a policy: the UI's
    Process and Resummarize buttons insert a pipeline for a completed episode
    on purpose. Only the *automatic* discovery is gated."""
    pid = _subscribed_podcast(conn)
    ep = _new_episode(conn, pid, "ep-done")
    worker = Worker(conn, _cfg())
    _sync(worker)
    assert find_new_episodes(conn) == []

    assert stages.summary_writes == 1

    # Plain manual enqueue on a summarized episode: allowed; the jobs run and
    # short-circuit on the existing artifacts.
    assert enqueue_episode_pipeline(conn, ep) is not None
    _sync(worker)
    assert _job_count(conn, ep) == 4
    assert stages.summary_writes == 1

    # Resummarize: forced summarize regenerates in place.
    assert enqueue_episode_pipeline(conn, ep, force_summarize=True) is not None
    _sync(worker)
    assert _job_count(conn, ep) == 6
    assert stages.summary_writes == 2

    # And neither manual pipeline makes discovery start churning again.
    assert find_new_episodes(conn) == []


def _podcast_with_failed_newest(conn, stages, cfg: Config) -> tuple[int, int, int]:
    """A subscribed podcast whose newest episode's pipeline just failed under
    ``cfg`` and an older, untouched backlog episode. Returns (pid, newest, older)."""
    pid = _subscribed_podcast(conn)
    upsert_episode(conn, pid, feed_ep("older"))
    older = conn.execute("SELECT id FROM episodes WHERE guid='older'").fetchone()["id"]
    set_episode_created_at(conn, older, "2026-05-01 00:00:00")  # backlog: pre-subscribe
    newest = _new_episode(conn, pid, "newest-403")
    conn.execute("UPDATE episodes SET published_at='2026-05-21T00:00:00' WHERE id=?", (newest,))
    conn.execute("UPDATE episodes SET published_at='2024-01-01T00:00:00' WHERE id=?", (older,))
    conn.commit()

    stages.fail_transcribe = "download failed: HTTP 403"
    _sync(Worker(conn, cfg))
    assert _job_count(conn, newest) == 2
    return pid, newest, older


def test_subscribe_autoqueue_skips_newest_within_the_cooldown(conn, stages):
    """queue_latest_unprocessed_episode shares the predicate: on subscribe,
    fall past an episode that just failed rather than re-run it inside the
    cooldown — and come back to it once the cooldown has passed."""
    cfg = _cfg(max_attempts=1, auto_retry_cooldown_hours=6)
    pid, newest, older = _podcast_with_failed_newest(conn, stages, cfg)

    assert queue_latest_unprocessed_episode(conn, cfg, pid) == older

    _age_failures(conn, newest, hours=7)
    # older is now in flight (excluded); newest is eligible again.
    assert queue_latest_unprocessed_episode(conn, cfg, pid) == newest


def test_subscribe_autoqueue_skips_newest_once_the_budget_is_spent(conn, stages):
    """Same path, budget of one: a spent budget keeps the newest episode out
    even with the cooldown long gone."""
    cfg = _cfg(max_attempts=1, auto_retry_pipelines=1, auto_retry_cooldown_hours=NO_COOLDOWN)
    pid, newest, older = _podcast_with_failed_newest(conn, stages, cfg)

    _age_failures(conn, newest, hours=48)
    assert queue_latest_unprocessed_episode(conn, cfg, pid) == older
    assert queue_latest_unprocessed_episode(conn, cfg, pid) is None  # older in flight
