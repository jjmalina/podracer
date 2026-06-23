import sqlite3

from podracer.db.connection import get_config, set_config
from podracer.models import Job

WATERMARK_KEY = "worker_watermark"
LAST_SYNC_KEY = "worker_last_sync"

# Statuses that count as an episode having an in-flight job. The correlated
# subselect below is the single place the listing queries (db/episodes.py) get
# the "active job kind"; get_active_kind is its standalone form for callers that
# already have an episode id.
ACTIVE_KIND_SUBSELECT = (
    "(SELECT j.kind FROM jobs j "
    "WHERE j.episode_id = e.id AND j.status IN ('queued', 'running') "
    "ORDER BY j.id LIMIT 1)"
)


def _from_row(row: sqlite3.Row) -> Job:
    return Job(**{k: row[k] for k in row.keys()})


def get_active_kind(conn: sqlite3.Connection, episode_id: int) -> str | None:
    """Kind of the episode's in-flight (queued/running) job, or None."""
    row = conn.execute(
        "SELECT kind FROM jobs WHERE episode_id = ? "
        "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
        (episode_id,),
    ).fetchone()
    return row["kind"] if row else None


# ---------- Watermark + sync timestamp ----------

def init_worker_watermark(conn: sqlite3.Connection) -> str:
    """Set watermark to now() if not set. Return current watermark."""
    existing = get_config(conn, WATERMARK_KEY)
    if existing:
        return existing
    now = conn.execute("SELECT datetime('now') AS ts").fetchone()["ts"]
    set_config(conn, WATERMARK_KEY, now)
    return now


def get_worker_watermark(conn: sqlite3.Connection) -> str | None:
    return get_config(conn, WATERMARK_KEY)


def set_worker_watermark(conn: sqlite3.Connection, iso_ts: str) -> None:
    set_config(conn, WATERMARK_KEY, iso_ts)


def set_worker_last_sync(conn: sqlite3.Connection, iso_ts: str) -> None:
    set_config(conn, LAST_SYNC_KEY, iso_ts)


def get_worker_last_sync(conn: sqlite3.Connection) -> str | None:
    return get_config(conn, LAST_SYNC_KEY)


# ---------- Enqueue + discovery ----------

def enqueue_episode_pipeline(
    conn: sqlite3.Connection, episode_id: int, max_attempts: int = 3,
) -> tuple[int, int] | None:
    """Insert a transcribe job, then a summarize job depending on it.

    Returns (transcribe_job_id, summarize_job_id), or None if either kind is
    already active (queued/running) for this episode.
    """
    try:
        cur = conn.execute(
            "INSERT INTO jobs (episode_id, kind, max_attempts) VALUES (?, 'transcribe', ?)",
            (episode_id, max_attempts),
        )
        transcribe_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO jobs (episode_id, kind, depends_on_job_id, max_attempts) "
            "VALUES (?, 'summarize', ?, ?)",
            (episode_id, transcribe_id, max_attempts),
        )
        summarize_id = cur.lastrowid
        conn.commit()
        return (transcribe_id, summarize_id) if transcribe_id and summarize_id else None
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def find_new_episodes(conn: sqlite3.Connection) -> list[int]:
    """Episodes that should be auto-enqueued by the worker.

    Returns episode ids where:
      - the podcast is subscribed AND has a subscribed_at watermark
      - the episode's created_at is after the podcast's subscribed_at
      - no active (queued/running) job exists for that episode yet
    """
    rows = conn.execute(
        """SELECT e.id FROM episodes e
           JOIN podcasts p ON p.id = e.podcast_id
           WHERE p.subscribed = 1
             AND p.subscribed_at IS NOT NULL
             AND e.created_at > p.subscribed_at
             AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.episode_id = e.id
                  AND j.status IN ('queued', 'running')
             )
           ORDER BY e.created_at""",
    ).fetchall()
    return [r["id"] for r in rows]


# ---------- Drain ----------

def claim_next_job(conn: sqlite3.Connection) -> Job | None:
    """Atomically transition the oldest queued job whose dep is satisfied
    (or has no dep) to 'running'. Returns the claimed job or None."""
    row = conn.execute(
        """UPDATE jobs
           SET status = 'running', started_at = datetime('now')
           WHERE id = (
               SELECT j.id FROM jobs j
               LEFT JOIN jobs d ON d.id = j.depends_on_job_id
               WHERE j.status = 'queued'
                 AND (j.depends_on_job_id IS NULL OR d.status = 'done')
               ORDER BY j.created_at
               LIMIT 1
           )
           RETURNING *""",
    ).fetchone()
    conn.commit()
    return _from_row(row) if row else None


def mark_job_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        """UPDATE jobs SET status = 'done', finished_at = datetime('now'),
                          last_error = NULL
           WHERE id = ?""",
        (job_id,),
    )
    conn.commit()


def mark_job_failed(conn: sqlite3.Connection, job_id: int, error: str) -> bool:
    """Increment attempts. Requeue if under max; otherwise mark failed.
    Returns True if the job is now terminal (status='failed')."""
    row = conn.execute(
        "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,),
    ).fetchone()
    if not row:
        return False
    new_attempts = row["attempts"] + 1
    if new_attempts < row["max_attempts"]:
        conn.execute(
            """UPDATE jobs SET attempts = ?, status = 'queued',
                              last_error = ?, started_at = NULL
               WHERE id = ?""",
            (new_attempts, error, job_id),
        )
        conn.commit()
        return False
    conn.execute(
        """UPDATE jobs SET attempts = ?, status = 'failed',
                          last_error = ?, finished_at = datetime('now')
           WHERE id = ?""",
        (new_attempts, error, job_id),
    )
    conn.commit()
    return True


def cascade_block_dependents(conn: sqlite3.Connection, failed_job_id: int) -> int:
    """Mark queued jobs depending (transitively) on a failed job as 'blocked'."""
    blocked = 0
    frontier = [failed_job_id]
    while frontier:
        parent = frontier.pop()
        rows = conn.execute(
            "SELECT id FROM jobs WHERE depends_on_job_id = ? AND status = 'queued'",
            (parent,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE jobs SET status = 'blocked', "
                "last_error = 'upstream dependency failed' WHERE id = ?",
                (r["id"],),
            )
            blocked += 1
            frontier.append(r["id"])
    conn.commit()
    return blocked


def reset_running_jobs(conn: sqlite3.Connection) -> int:
    """On worker startup: requeue any jobs left in 'running' state."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', started_at = NULL, "
        "last_error = 'worker restarted mid-job' WHERE status = 'running'"
    )
    conn.commit()
    return cur.rowcount


# ---------- Observability ----------

def get_job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "blocked": 0}
    for r in rows:
        counts[r["status"]] = r["n"]
    return counts


def get_running_jobs(conn: sqlite3.Connection) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at"
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_queued_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' "
        "ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_done_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'done' "
        "ORDER BY finished_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_failed_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'failed' "
        "ORDER BY finished_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_blocked_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'blocked' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _from_row(row) if row else None


def retry_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Reset a failed job to queued. Also unblock any cascade-blocked
    dependents so the chain has a chance to flow again. Returns True if
    the job was failed and got requeued."""
    row = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,),
    ).fetchone()
    if not row or row["status"] != "failed":
        return False
    conn.execute(
        "UPDATE jobs SET status='queued', attempts=0, last_error=NULL, "
        "started_at=NULL, finished_at=NULL WHERE id = ?",
        (job_id,),
    )
    # Unblock anything that depended on this job (transitively).
    frontier = [job_id]
    while frontier:
        parent = frontier.pop()
        deps = conn.execute(
            "SELECT id FROM jobs WHERE depends_on_job_id = ? AND status = 'blocked'",
            (parent,),
        ).fetchall()
        for d in deps:
            conn.execute(
                "UPDATE jobs SET status='queued', last_error=NULL WHERE id = ?",
                (d["id"],),
            )
            frontier.append(d["id"])
    conn.commit()
    return True


def cancel_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Delete a queued job AND any dependents (which would orphan otherwise).
    Returns True if anything was deleted."""
    row = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,),
    ).fetchone()
    if not row or row["status"] not in ("queued", "blocked"):
        return False
    to_delete = [job_id]
    frontier = [job_id]
    while frontier:
        parent = frontier.pop()
        deps = conn.execute(
            "SELECT id FROM jobs WHERE depends_on_job_id = ?", (parent,),
        ).fetchall()
        for d in deps:
            to_delete.append(d["id"])
            frontier.append(d["id"])
    placeholders = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", to_delete)
    conn.commit()
    return True
