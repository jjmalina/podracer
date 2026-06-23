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
