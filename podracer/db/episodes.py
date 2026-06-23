import sqlite3

from podracer.db.jobs import ACTIVE_KIND_SUBSELECT
from podracer.db.podcasts import tag_filter_clause
from podracer.models import Episode, EpisodeListItem, FeedEpisode, FeedItem


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


# Shared feed predicate, used by both the listing and the count query so their
# filters can't drift. Param order: subscribed_only(int), status, status,
# topic, topic. (? IS NULL) disables a filter; the topic clause matches shows
# carrying that tag (t.name is COLLATE NOCASE so case doesn't matter).
_FEED_WHERE = """
    FROM episodes e
    JOIN podcasts p ON p.id = e.podcast_id
    WHERE (? = 0 OR p.subscribed = 1)
      AND (? IS NULL OR e.status = ?)
      AND (? IS NULL OR EXISTS (
            SELECT 1 FROM podcast_tags pt JOIN tags t ON t.id = pt.tag_id
            WHERE pt.podcast_id = p.id AND t.name = ?))
"""

# Newest episodes across all shows (the home feed). The active-job subselect
# is a correlated LEFT-style lookup — a row appears even when no job exists.
# Sort key COALESCE(published_at, created_at) is never NULL (created_at has a
# NOT NULL default); the id DESC tiebreak keeps paging windows stable when two
# episodes share a timestamp.
_RECENT_SELECT = f"""
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
        {ACTIVE_KIND_SUBSELECT} AS active_kind
    {_FEED_WHERE}
    ORDER BY recency DESC, e.id DESC
    LIMIT ? OFFSET ?
"""


def _feed_item_from_row(row: sqlite3.Row) -> FeedItem:
    return FeedItem(**{k: row[k] for k in row.keys()})


def _none_if_all(value: str | None) -> str | None:
    """None or 'all' means no filter; anything else is an exact match."""
    return value if value and value != "all" else None


def get_recent_episodes(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int = 0,
    subscribed_only: bool = True,
    status: str | None = None,
    topic: str | None = None,
) -> list[FeedItem]:
    """Episodes across all (or only subscribed) shows, newest first.

    status filters on episodes.status (e.g. 'summarized'); None/'all' = no filter.
    topic filters on the show's topic tags; None/'all' = no filter.
    """
    sf = _none_if_all(status)
    tf = _none_if_all(topic)
    rows = conn.execute(
        _RECENT_SELECT,
        (int(subscribed_only), sf, sf, tf, tf, int(limit), int(offset)),
    ).fetchall()
    return [_feed_item_from_row(r) for r in rows]


def count_recent_episodes(
    conn: sqlite3.Connection, *, subscribed_only: bool = True,
    status: str | None = None, topic: str | None = None,
) -> int:
    """Total rows the feed would show — for page count. Mirrors the WHERE above."""
    sf = _none_if_all(status)
    tf = _none_if_all(topic)
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt {_FEED_WHERE}",
        (int(subscribed_only), sf, sf, tf, tf),
    ).fetchone()
    return row["cnt"]


# --- JSON API episode listing ------------------------------------------------
# The API list differs from the home feed in three ways: it can filter to one
# show (podcast_id), it accepts *multiple* tags (OR-semantics, via the shared
# tag_filter_clause), and it surfaces has_summary / has_transcript flags plus
# the optional embedded summary. The WHERE clause is built dynamically because
# the tag IN (...) placeholder count varies, so it's a builder rather than the
# static _FEED_WHERE string above.


def _api_where(
    *, subscribed_only: bool, status: str | None,
    tags: list[str] | None, podcast_id: int | None,
) -> tuple[str, list]:
    """Build the shared WHERE clause + params for the API list and count.

    Status is an exact match (None/'all' = no filter); tags use the shared
    OR-semantics EXISTS predicate. Params are bound, never interpolated.
    """
    clauses: list[str] = []
    params: list = []
    if subscribed_only:
        clauses.append("p.subscribed = 1")
    if podcast_id is not None:
        clauses.append("e.podcast_id = ?")
        params.append(int(podcast_id))
    sf = _none_if_all(status)
    if sf is not None:
        clauses.append("e.status = ?")
        params.append(sf)
    tag_clause, tag_params = tag_filter_clause(tags)
    if tag_clause:
        clauses.append(tag_clause)
        params.extend(tag_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _episode_list_item_from_row(row: sqlite3.Row) -> EpisodeListItem:
    return EpisodeListItem(**{k: row[k] for k in row.keys()})


def list_episodes(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int = 0,
    podcast_id: int | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    subscribed_only: bool = True,
    include_summary: bool = False,
) -> list[EpisodeListItem]:
    """Episodes for the JSON API, newest first, with existence flags.

    podcast_id scopes to one show; tags is an OR-set of topic names; status is an
    exact episodes.status match (None/'all' = no filter). has_summary /
    has_transcript come from LEFT JOINs in the same query (no N+1). When
    include_summary is set, the raw PodcastSummary JSON rides along in
    summary_data for the route to parse.
    """
    where, params = _api_where(
        subscribed_only=subscribed_only, status=status, tags=tags, podcast_id=podcast_id,
    )
    summary_col = "s.data AS summary_data" if include_summary else "NULL AS summary_data"
    sql = f"""
        SELECT
            e.id AS id,
            e.podcast_id AS podcast_id,
            e.title AS title,
            e.published_at AS published_at,
            e.created_at AS created_at,
            e.status AS status,
            e.duration_seconds AS duration_seconds,
            p.title AS podcast_title,
            {ACTIVE_KIND_SUBSELECT} AS active_kind,
            (s.episode_id IS NOT NULL) AS has_summary,
            (t.episode_id IS NOT NULL) AS has_transcript,
            {summary_col}
        FROM episodes e
        JOIN podcasts p ON p.id = e.podcast_id
        LEFT JOIN summaries s ON s.episode_id = e.id
        LEFT JOIN transcripts t ON t.episode_id = e.id
        {where}
        ORDER BY COALESCE(e.published_at, e.created_at) DESC, e.id DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
    return [_episode_list_item_from_row(r) for r in rows]


def count_episodes(
    conn: sqlite3.Connection,
    *,
    podcast_id: int | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    subscribed_only: bool = True,
) -> int:
    """Total rows list_episodes would return — for pagination. Same WHERE."""
    where, params = _api_where(
        subscribed_only=subscribed_only, status=status, tags=tags, podcast_id=podcast_id,
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM episodes e JOIN podcasts p ON p.id = e.podcast_id{where}",
        params,
    ).fetchone()
    return row["cnt"]
