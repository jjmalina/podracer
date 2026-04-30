import sqlite3
from pathlib import Path

from podracer.models import Episode, FeedEpisode, Podcast, SummaryRecord, Transcript

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
"""

DEFAULT_CONFIG = {
    "media_dir": DEFAULT_MEDIA_DIR,
}


def _podcast_from_row(row: sqlite3.Row) -> Podcast:
    return Podcast(**{k: row[k] for k in row.keys()})


def _episode_from_row(row: sqlite3.Row) -> Episode:
    return Episode(**{k: row[k] for k in row.keys()})


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def upsert_podcast(conn: sqlite3.Connection, title: str, author: str | None,
                   feed_url: str, artwork_url: str | None = None,
                   description: str | None = None) -> int:
    conn.execute(
        """INSERT INTO podcasts (title, author, feed_url, artwork_url, description)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(feed_url) DO UPDATE SET
             title=excluded.title, author=excluded.author,
             artwork_url=excluded.artwork_url,
             description=excluded.description""",
        (title, author, feed_url, artwork_url, description),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
    return row["id"]


def subscribe(conn: sqlite3.Connection, podcast_id: int) -> None:
    conn.execute("UPDATE podcasts SET subscribed = 1 WHERE id = ?", (podcast_id,))
    conn.commit()


def unsubscribe(conn: sqlite3.Connection, podcast_id: int) -> None:
    conn.execute("UPDATE podcasts SET subscribed = 0 WHERE id = ?", (podcast_id,))
    conn.commit()


def get_podcast(conn: sqlite3.Connection, podcast_id: int) -> Podcast | None:
    row = conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    return _podcast_from_row(row) if row else None


def get_subscribed_podcasts(conn: sqlite3.Connection) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts WHERE subscribed = 1").fetchall()
    return [_podcast_from_row(r) for r in rows]


def upsert_episode(conn: sqlite3.Connection, podcast_id: int, ep: FeedEpisode) -> None:
    conn.execute(
        """INSERT INTO episodes (podcast_id, guid, title, audio_url, published_at,
                                 duration_seconds, description)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(podcast_id, guid) DO UPDATE SET
             title=excluded.title, audio_url=excluded.audio_url,
             published_at=excluded.published_at,
             duration_seconds=excluded.duration_seconds,
             description=excluded.description""",
        (podcast_id, ep.guid, ep.title, ep.audio_url, ep.published_at,
         ep.duration_seconds, ep.description),
    )


def get_episodes(conn: sqlite3.Connection, podcast_id: int,
                 limit: int | None = None) -> list[Episode]:
    query = "SELECT * FROM episodes WHERE podcast_id = ? ORDER BY published_at DESC"
    if limit:
        query += f" LIMIT {limit}"
    rows = conn.execute(query, (podcast_id,)).fetchall()
    return [_episode_from_row(r) for r in rows]


def get_episode(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return _episode_from_row(row) if row else None


def update_episode_download(conn: sqlite3.Connection, episode_id: int,
                            local_path: str, file_size_bytes: int) -> None:
    conn.execute(
        """UPDATE episodes SET local_path = ?, file_size_bytes = ?, status = 'downloaded'
           WHERE id = ?""",
        (local_path, file_size_bytes, episode_id),
    )
    conn.commit()


def _transcript_from_row(row: sqlite3.Row) -> Transcript:
    return Transcript(**{k: row[k] for k in row.keys()})


def save_transcript(conn: sqlite3.Connection, episode_id: int, text: str,
                    model: str, language: str | None = None) -> int:
    conn.execute(
        """INSERT INTO transcripts (episode_id, text, model, language)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(episode_id) DO UPDATE SET
             text=excluded.text, model=excluded.model,
             language=excluded.language,
             created_at=datetime('now')""",
        (episode_id, text, model, language),
    )
    conn.execute(
        "UPDATE episodes SET status = 'transcribed' WHERE id = ?",
        (episode_id,),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM transcripts WHERE episode_id = ?", (episode_id,)).fetchone()
    return row["id"]


def get_transcript(conn: sqlite3.Connection, episode_id: int) -> Transcript | None:
    row = conn.execute("SELECT * FROM transcripts WHERE episode_id = ?", (episode_id,)).fetchone()
    return _transcript_from_row(row) if row else None


def _summary_from_row(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(**{k: row[k] for k in row.keys()})


def save_summary(conn: sqlite3.Connection, episode_id: int, data: str,
                 model: str, backend: str) -> int:
    conn.execute(
        """INSERT INTO summaries (episode_id, data, model, backend)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(episode_id) DO UPDATE SET
             data=excluded.data, model=excluded.model,
             backend=excluded.backend, created_at=datetime('now')""",
        (episode_id, data, model, backend),
    )
    conn.execute(
        "UPDATE episodes SET status = 'summarized' WHERE id = ?",
        (episode_id,),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM summaries WHERE episode_id = ?", (episode_id,)).fetchone()
    return row["id"]


def get_summary(conn: sqlite3.Connection, episode_id: int) -> SummaryRecord | None:
    row = conn.execute("SELECT * FROM summaries WHERE episode_id = ?", (episode_id,)).fetchone()
    return _summary_from_row(row) if row else None


def update_podcast_synced(conn: sqlite3.Connection, podcast_id: int) -> None:
    conn.execute(
        "UPDATE podcasts SET last_synced_at = datetime('now') WHERE id = ?",
        (podcast_id,),
    )
    conn.commit()
