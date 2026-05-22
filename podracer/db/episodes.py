import sqlite3

from podracer.models import Episode, FeedEpisode


def _from_row(row: sqlite3.Row) -> Episode:
    return Episode(**{k: row[k] for k in row.keys()})


def upsert_episode(conn: sqlite3.Connection, podcast_id: int, ep: FeedEpisode) -> None:
    conn.execute(
        """INSERT INTO episodes (podcast_id, guid, title, audio_url, published_at,
                                 duration_seconds, description, show_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(podcast_id, guid) DO UPDATE SET
             title=excluded.title, audio_url=excluded.audio_url,
             published_at=excluded.published_at,
             duration_seconds=excluded.duration_seconds,
             description=excluded.description,
             show_notes=excluded.show_notes""",
        (podcast_id, ep.guid, ep.title, ep.audio_url, ep.published_at,
         ep.duration_seconds, ep.description, ep.show_notes),
    )


def get_episodes(
    conn: sqlite3.Connection, podcast_id: int, limit: int | None = None,
) -> list[Episode]:
    query = "SELECT * FROM episodes WHERE podcast_id = ? ORDER BY published_at DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, (podcast_id,)).fetchall()
    return [_from_row(r) for r in rows]


def get_episode(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return _from_row(row) if row else None


def get_episode_count(conn: sqlite3.Connection, podcast_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM episodes WHERE podcast_id = ?", (podcast_id,),
    ).fetchone()
    return row["cnt"]


def update_episode_download(
    conn: sqlite3.Connection, episode_id: int, local_path: str, file_size_bytes: int,
) -> None:
    conn.execute(
        """UPDATE episodes SET local_path = ?, file_size_bytes = ?, status = 'downloaded'
           WHERE id = ?""",
        (local_path, file_size_bytes, episode_id),
    )
    conn.commit()
