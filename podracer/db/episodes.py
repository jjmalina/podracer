import sqlite3

from podracer.models import Episode, FeedEpisode, FeedItem


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


# Newest episodes across all shows (the home feed). The active-job subselect
# is a correlated LEFT-style lookup — a row appears even when no job exists.
# Sort key COALESCE(published_at, created_at) is never NULL (created_at has a
# NOT NULL default); the id DESC tiebreak keeps paging windows stable when two
# episodes share a timestamp.
_RECENT_SELECT = """
    SELECT
        e.id AS id,
        e.podcast_id AS podcast_id,
        e.title AS title,
        e.published_at AS published_at,
        e.created_at AS created_at,
        COALESCE(e.published_at, e.created_at) AS recency,
        e.status AS status,
        e.duration_seconds AS duration_seconds,
        p.title AS podcast_title,
        (SELECT j.kind FROM jobs j
           WHERE j.episode_id = e.id AND j.status IN ('queued', 'running')
           ORDER BY j.id LIMIT 1) AS active_kind
    FROM episodes e
    JOIN podcasts p ON p.id = e.podcast_id
    WHERE (? = 0 OR p.subscribed = 1)
      AND (? IS NULL OR e.status = ?)
    ORDER BY recency DESC, e.id DESC
    LIMIT ? OFFSET ?
"""


def _feed_item_from_row(row: sqlite3.Row) -> FeedItem:
    return FeedItem(**{k: row[k] for k in row.keys()})


def _status_filter(status: str | None) -> str | None:
    """None or 'all' means no status filter; anything else is an exact match."""
    return status if status and status != "all" else None


def get_recent_episodes(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int = 0,
    subscribed_only: bool = True,
    status: str | None = None,
) -> list[FeedItem]:
    """Episodes across all (or only subscribed) shows, newest first.

    status filters on episodes.status (e.g. 'summarized'); None/'all' = no filter.
    """
    sf = _status_filter(status)
    rows = conn.execute(
        _RECENT_SELECT, (int(subscribed_only), sf, sf, int(limit), int(offset)),
    ).fetchall()
    return [_feed_item_from_row(r) for r in rows]


def count_recent_episodes(
    conn: sqlite3.Connection, *, subscribed_only: bool = True, status: str | None = None,
) -> int:
    """Total rows the feed would show — for page count. Mirrors the WHERE above."""
    sf = _status_filter(status)
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM episodes e JOIN podcasts p ON p.id = e.podcast_id "
        "WHERE (? = 0 OR p.subscribed = 1) AND (? IS NULL OR e.status = ?)",
        (int(subscribed_only), sf, sf),
    ).fetchone()
    return row["cnt"]
