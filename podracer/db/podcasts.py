import sqlite3

from podracer.models import Podcast


def _from_row(row: sqlite3.Row) -> Podcast:
    return Podcast(**{k: row[k] for k in row.keys()})


def upsert_podcast(
    conn: sqlite3.Connection,
    title: str,
    author: str | None,
    feed_url: str,
    artwork_url: str | None = None,
    description: str | None = None,
) -> int:
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
    # subscribed_at is the per-podcast watermark — only episodes whose
    # created_at is later than this get auto-enqueued by the worker.
    conn.execute(
        "UPDATE podcasts SET subscribed = 1, subscribed_at = datetime('now') "
        "WHERE id = ?",
        (podcast_id,),
    )
    conn.commit()


def unsubscribe(conn: sqlite3.Connection, podcast_id: int) -> None:
    conn.execute("UPDATE podcasts SET subscribed = 0 WHERE id = ?", (podcast_id,))
    conn.commit()


def get_podcast(conn: sqlite3.Connection, podcast_id: int) -> Podcast | None:
    row = conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    return _from_row(row) if row else None


def get_subscribed_podcasts(conn: sqlite3.Connection) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts WHERE subscribed = 1").fetchall()
    return [_from_row(r) for r in rows]


def get_all_podcasts(conn: sqlite3.Connection) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts ORDER BY title").fetchall()
    return [_from_row(r) for r in rows]


def update_podcast_synced(conn: sqlite3.Connection, podcast_id: int) -> None:
    conn.execute(
        "UPDATE podcasts SET last_synced_at = datetime('now') WHERE id = ?",
        (podcast_id,),
    )
    conn.commit()


def set_podcast_artwork_path(conn: sqlite3.Connection, podcast_id: int, path: str) -> None:
    conn.execute(
        "UPDATE podcasts SET artwork_path = ? WHERE id = ?",
        (path, podcast_id),
    )
    conn.commit()
