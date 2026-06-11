"""Long-running worker loop: sync feeds, enqueue new episodes, drain jobs."""
import signal
import sqlite3
import threading
import time
from datetime import UTC, datetime

from podracer import logger
from podracer.config import Config
from podracer.db import (
    cascade_block_dependents,
    claim_next_job,
    enqueue_episode_pipeline,
    find_new_episodes,
    get_subscribed_podcasts,
    init_worker_watermark,
    mark_job_done,
    mark_job_failed,
    reset_running_jobs,
    set_worker_last_sync,
    set_worker_watermark,
    update_podcast_synced,
    upsert_episode,
)
from podracer.feed import fetch_episodes
from podracer.models import Job
from podracer.process import summarize_episode, transcribe_episode


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sqlite_now(conn: sqlite3.Connection) -> str:
    """Match the format the schema uses for created_at columns."""
    return conn.execute("SELECT datetime('now') AS ts").fetchone()["ts"]


class Worker:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.shutdown = threading.Event()

    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            logger.info("received signal %d, shutting down gracefully", signum)
            self.shutdown.set()
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    def run_once(self) -> None:
        """Single iteration: sync feeds, enqueue, drain queue. Used by --once."""
        self._sync_feeds()
        self._enqueue_new()
        self._drain_queue()

    def run_forever(self) -> None:
        """Long-running loop. Drains the queue frequently (drain_interval_seconds)
        so UI-queued work picks up fast; syncs feeds periodically
        (sync_interval_minutes) since RSS doesn't change in seconds."""
        requeued = reset_running_jobs(self.conn)
        if requeued:
            logger.info("orphan recovery: requeued %d running job(s)", requeued)
        init_worker_watermark(self.conn)

        sync_interval = self.cfg.sync_interval_minutes * 60
        drain_interval = self.cfg.drain_interval_seconds
        last_sync = 0.0  # forces a sync on the first iteration

        while not self.shutdown.is_set():
            now = time.monotonic()
            try:
                if now - last_sync >= sync_interval:
                    self._sync_feeds()
                    self._enqueue_new()
                    last_sync = now
                self._drain_queue()
            except Exception:
                logger.exception("worker iteration failed")
            self.shutdown.wait(timeout=drain_interval)

    # --- internals ---

    def _sync_feeds(self) -> None:
        podcasts = get_subscribed_podcasts(self.conn)
        for podcast in podcasts:
            if self.shutdown.is_set():
                return
            try:
                episodes = fetch_episodes(podcast.feed_url, limit=10)
                for ep in episodes:
                    upsert_episode(self.conn, podcast.id, ep)
                # One transaction per podcast: update_podcast_synced commits
                # the upserts and the last_synced_at bump together.
                update_podcast_synced(self.conn, podcast.id)
                logger.info("synced %s (%d episodes)", podcast.title, len(episodes))
            except Exception:
                # Drop any partial batch — without this, pending upserts would
                # ride along in whatever commit happens next on this connection.
                self.conn.rollback()
                logger.exception("feed sync failed: %s", podcast.title)
        set_worker_last_sync(self.conn, _utcnow_iso())

    def _enqueue_new(self) -> None:
        # find_new_episodes uses each podcast's subscribed_at watermark — only
        # episodes that arrived in the DB after subscribing get auto-enqueued.
        new_ids = find_new_episodes(self.conn)
        for episode_id in new_ids:
            if self.shutdown.is_set():
                return
            result = enqueue_episode_pipeline(
                self.conn, episode_id, max_attempts=self.cfg.max_attempts,
            )
            if result:
                logger.info("enqueued episode %d (transcribe=%d, summarize=%d)",
                            episode_id, result[0], result[1])
        # Keep the global watermark advancing for `podracer status` visibility.
        set_worker_watermark(self.conn, _sqlite_now(self.conn))

    def _drain_queue(self) -> None:
        while not self.shutdown.is_set():
            job = claim_next_job(self.conn)
            if job is None:
                return
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        logger.info("running job %d: %s for episode %d (attempt %d/%d)",
                    job.id, job.kind, job.episode_id,
                    job.attempts + 1, job.max_attempts)
        try:
            self._dispatch(job)
            mark_job_done(self.conn, job.id)
            logger.info("job %d done", job.id)
        except Exception as e:
            logger.exception("job %d failed: %s", job.id, e)
            terminal = mark_job_failed(self.conn, job.id, str(e))
            if terminal:
                blocked = cascade_block_dependents(self.conn, job.id)
                logger.warning("job %d exhausted retries; blocked %d dependents",
                               job.id, blocked)

    def _dispatch(self, job: Job) -> None:
        if job.kind == "transcribe":
            transcribe_episode(self.conn, self.cfg, job.episode_id)
        elif job.kind == "summarize":
            summarize_episode(self.conn, self.cfg, job.episode_id)
        else:
            raise ValueError(f"unknown job kind: {job.kind}")
