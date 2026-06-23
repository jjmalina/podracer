import sqlite3

from podracer.models import Podcast


def _from_row(row: sqlite3.Row) -> Podcast:
    return Podcast(**{k: row[k] for k in row.keys()})


def _attach_topics(conn: sqlite3.Connection, podcasts: list[Podcast]) -> list[Podcast]:
    """Populate each podcast's .topics in one batched query (avoids N+1)."""
    if not podcasts:
        return podcasts
    ids = [p.id for p in podcasts]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT pt.podcast_id AS podcast_id, t.name AS name
            FROM podcast_tags pt JOIN tags t ON t.id = pt.tag_id
            WHERE pt.podcast_id IN ({placeholders})
            ORDER BY t.name COLLATE NOCASE""",
        ids,
    ).fetchall()
    by_id: dict[int, list[str]] = {}
    for r in rows:
        by_id.setdefault(r["podcast_id"], []).append(r["name"])
    for p in podcasts:
        p.topics = by_id.get(p.id, [])
    return podcasts


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
    if not row:
        return None
    return _attach_topics(conn, [_from_row(row)])[0]


def get_subscribed_podcasts(conn: sqlite3.Connection) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts WHERE subscribed = 1").fetchall()
    return _attach_topics(conn, [_from_row(r) for r in rows])


def get_all_podcasts(conn: sqlite3.Connection) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts ORDER BY title").fetchall()
    return _attach_topics(conn, [_from_row(r) for r in rows])


def clean_tags(tags: list[str] | None) -> list[str] | None:
    """Drop blanks and the 'all' sentinel; None when nothing real remains.

    Shared by the podcast and episode tag filters so 'what counts as a real
    tag' has one definition."""
    if not tags:
        return None
    cleaned = [t.strip() for t in tags if t and t.strip() and t.strip().lower() != "all"]
    return cleaned or None


def tag_filter_clause(tags: list[str] | None) -> tuple[str, list]:
    """An EXISTS(...) predicate matching podcasts (aliased `p`) that carry any of
    `tags` — OR-semantics, case-insensitive (tags.name is COLLATE NOCASE). Names
    are bound, never interpolated. Returns ('', []) when there's no real tag, so
    callers can skip the clause.
    """
    cleaned = clean_tags(tags)
    if not cleaned:
        return "", []
    placeholders = ",".join("?" * len(cleaned))
    clause = (
        "EXISTS (SELECT 1 FROM podcast_tags pt JOIN tags t ON t.id = pt.tag_id "
        f"WHERE pt.podcast_id = p.id AND t.name IN ({placeholders}))"
    )
    return clause, cleaned


def _podcasts_where(
    *, subscribed_only: bool, tags: list[str] | None,
) -> tuple[str, list]:
    """Shared WHERE for the API podcast list + count. Tags are OR-semantics."""
    clauses: list[str] = []
    params: list = []
    if subscribed_only:
        clauses.append("p.subscribed = 1")
    tag_clause, tag_params = tag_filter_clause(tags)
    if tag_clause:
        clauses.append(tag_clause)
        params.extend(tag_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_podcasts(
    conn: sqlite3.Connection,
    *,
    tags: list[str] | None = None,
    subscribed_only: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[Podcast]:
    """Podcasts for the JSON API: optional tag (OR-set) filter + pagination.

    Distinct from get_all_podcasts / get_subscribed_podcasts, which take no
    filters and don't paginate. Topics are attached in one batched query.
    """
    where, params = _podcasts_where(subscribed_only=subscribed_only, tags=tags)
    # p.id tiebreaker so pages stay stable when two shows share a title.
    sql = f"SELECT p.* FROM podcasts p{where} ORDER BY p.title, p.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, int(limit), int(offset)]
    rows = conn.execute(sql, params).fetchall()
    return _attach_topics(conn, [_from_row(r) for r in rows])


def count_podcasts(
    conn: sqlite3.Connection, *, tags: list[str] | None = None, subscribed_only: bool = True,
) -> int:
    """Total rows get_podcasts would return — for pagination. Same WHERE."""
    where, params = _podcasts_where(subscribed_only=subscribed_only, tags=tags)
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM podcasts p{where}", params).fetchone()
    return row["cnt"]


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


def set_podcast_tags(conn: sqlite3.Connection, podcast_id: int, names: list[str]) -> None:
    """Replace a podcast's topic tags with `names` (canonical category names).

    Two deliberate no-ops keep this safe to call on every sync:

    - **Empty `names` is ignored.** Feeds intermittently drop their categories
      (transient host errors, a feed that omits <itunes:category>, or categories
      that aren't on the Apple whitelist). Wiping good tags on a category-less
      sync would be silent data loss, so we leave the existing tags untouched.
    - **Unchanged tag sets skip all writes.** iTunes categories almost never
      change, so we avoid the DELETE+INSERT churn (and redundant commits) when
      the desired set already matches what's stored.
    """
    desired: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            desired.append(name)
    if not desired:
        return

    current = {
        r["name"].lower()
        for r in conn.execute(
            "SELECT t.name AS name FROM podcast_tags pt JOIN tags t ON t.id = pt.tag_id "
            "WHERE pt.podcast_id = ?",
            (podcast_id,),
        ).fetchall()
    }
    if current == seen:
        return

    conn.execute("DELETE FROM podcast_tags WHERE podcast_id = ?", (podcast_id,))
    for name in desired:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO podcast_tags (podcast_id, tag_id) VALUES (?, ?)",
            (podcast_id, row["id"]),
        )
    conn.commit()


def get_all_tags(conn: sqlite3.Connection, subscribed_only: bool = True) -> list[str]:
    """Distinct topic names that are actually attached to a (subscribed) show."""
    query = (
        "SELECT DISTINCT t.name AS name FROM tags t "
        "JOIN podcast_tags pt ON pt.tag_id = t.id "
        "JOIN podcasts p ON p.id = pt.podcast_id"
    )
    if subscribed_only:
        query += " WHERE p.subscribed = 1"
    query += " ORDER BY t.name COLLATE NOCASE"
    return [r["name"] for r in conn.execute(query).fetchall()]
