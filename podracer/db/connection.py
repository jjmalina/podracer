import html
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "./data/podracer.db"
DEFAULT_MEDIA_DIR = "./data/media/"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    feed_url TEXT NOT NULL UNIQUE,
    artwork_url TEXT,
    description TEXT,
    subscribed INTEGER NOT NULL DEFAULT 0,
    subscribed_at TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id),
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    audio_url TEXT NOT NULL,
    duration_seconds INTEGER,
    description TEXT,
    show_notes TEXT,
    local_path TEXT,
    file_size_bytes INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(podcast_id, guid)
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL UNIQUE REFERENCES episodes(id),
    data TEXT NOT NULL,
    model TEXT NOT NULL,
    backend TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL UNIQUE REFERENCES episodes(id),
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    depends_on_job_id INTEGER REFERENCES jobs(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_depends_on ON jobs(depends_on_job_id);

-- One active (queued/running) job per (episode, kind).
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_unique
    ON jobs(episode_id, kind) WHERE status IN ('queued', 'running');
"""

DEFAULT_CONFIG = {
    "media_dir": DEFAULT_MEDIA_DIR,
}


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    ep_cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    if "show_notes" not in ep_cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN show_notes TEXT")

    pc_cols = {row[1] for row in conn.execute("PRAGMA table_info(podcasts)").fetchall()}
    if "subscribed_at" not in pc_cols:
        conn.execute("ALTER TABLE podcasts ADD COLUMN subscribed_at TEXT")
        # Backfill existing subscribed podcasts so their backlog doesn't get
        # auto-queued the next time the worker runs.
        conn.execute(
            "UPDATE podcasts SET subscribed_at = datetime('now') "
            "WHERE subscribed = 1 AND subscribed_at IS NULL"
        )

    # Decode HTML entities in stored text fields. Earlier feed-ingest stripped
    # tags but left entities (&amp;, &nbsp;) as literal characters. Idempotent:
    # only updates rows where unescaping actually changes the value.
    for table, col in (
        ("episodes", "title"),
        ("episodes", "description"),
        ("episodes", "show_notes"),
        ("podcasts", "title"),
        ("podcasts", "description"),
        ("podcasts", "author"),
    ):
        rows = conn.execute(
            f"SELECT id, {col} FROM {table} WHERE {col} LIKE '%&%;%'"
        ).fetchall()
        for row in rows:
            original = row[col]
            decoded = html.unescape(original)
            if decoded != original:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE id = ?", (decoded, row["id"])
                )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    for key, value in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
