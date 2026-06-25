"""Storage for daily/weekly digests + the period-membership query.

Like every db.* module this returns plain models (DigestRecord / DigestMemberRow)
and raw JSON strings; parsing DigestData and all timezone/period math live in
podracer.digest, so this layer never imports it (no cycle).
"""
import sqlite3

from podracer.models import DigestMemberRow, DigestRecord

# A period's members are the *summarized* episodes of *subscribed* shows whose
# recency key falls in the period's UTC window. The JOIN summaries is what makes
# membership "summarized episodes": an episode with no summary yet is simply not
# a member, and folds in later via the straggler regen (see the scheduler).
_MEMBER_WHERE = """
    FROM episodes e
    JOIN summaries s ON s.episode_id = e.id
    JOIN podcasts p ON p.id = e.podcast_id
    WHERE COALESCE(e.published_at, e.created_at) >= ?
      AND COALESCE(e.published_at, e.created_at) <  ?
      AND p.subscribed = 1
"""


def _record_from_row(row: sqlite3.Row) -> DigestRecord:
    return DigestRecord(**{k: row[k] for k in row.keys()})


def save_digest(
    conn: sqlite3.Connection, kind: str, period_start: str, period_end: str,
    data: str, episode_count: int, model: str, backend: str,
) -> int:
    """Upsert a digest on (kind, period_start). Regeneration overwrites the
    stored snapshot + count + created_at, exactly like save_summary."""
    with conn:
        row = conn.execute(
            """INSERT INTO digests
                 (kind, period_start, period_end, data, episode_count, model, backend)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, period_start) DO UPDATE SET
                 period_end=excluded.period_end, data=excluded.data,
                 episode_count=excluded.episode_count, model=excluded.model,
                 backend=excluded.backend, created_at=datetime('now')
               RETURNING id""",
            (kind, period_start, period_end, data, episode_count, model, backend),
        ).fetchone()
    return row["id"]


def get_digest(conn: sqlite3.Connection, kind: str, period_start: str) -> DigestRecord | None:
    row = conn.execute(
        "SELECT * FROM digests WHERE kind = ? AND period_start = ?",
        (kind, period_start),
    ).fetchone()
    return _record_from_row(row) if row else None


def digest_exists(conn: sqlite3.Connection, kind: str, period_start: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM digests WHERE kind = ? AND period_start = ? LIMIT 1",
        (kind, period_start),
    ).fetchone()
    return row is not None


def get_digests(
    conn: sqlite3.Connection, *, kind: str | None = None,
    limit: int, offset: int = 0,
) -> list[DigestRecord]:
    """Digests newest-first for the feed, optionally one kind. period_start DESC
    is reverse-chronological; the id tiebreak keeps paging stable."""
    where, params = ("WHERE kind = ?", [kind]) if kind else ("", [])
    rows = conn.execute(
        f"SELECT * FROM digests {where} "
        "ORDER BY period_start DESC, id DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    ).fetchall()
    return [_record_from_row(r) for r in rows]


def count_digests(conn: sqlite3.Connection, *, kind: str | None = None) -> int:
    where, params = ("WHERE kind = ?", [kind]) if kind else ("", [])
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM digests {where}", params).fetchone()
    return row["cnt"]


def get_digest_members(
    conn: sqlite3.Connection, utc_lo: str, utc_hi: str,
) -> list[DigestMemberRow]:
    """Summarized episodes whose recency falls in [utc_lo, utc_hi). Newest first
    so the digest builder's per-show episode order is recency-desc by insertion."""
    rows = conn.execute(
        f"""SELECT e.id AS episode_id, e.podcast_id AS podcast_id,
                   e.title AS title, p.title AS podcast_title,
                   COALESCE(e.published_at, e.created_at) AS recency,
                   s.data AS summary_data
            {_MEMBER_WHERE}
            ORDER BY COALESCE(e.published_at, e.created_at) DESC, e.id DESC""",
        (utc_lo, utc_hi),
    ).fetchall()
    return [DigestMemberRow(**{k: r[k] for k in r.keys()}) for r in rows]


def count_digest_members(conn: sqlite3.Connection, utc_lo: str, utc_hi: str) -> int:
    """The cheap COUNT form of the membership query — the staleness probe the
    scheduler runs per recent period. summaries.episode_id is UNIQUE and joined
    1:1, so COUNT(*) is the distinct-episode count."""
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt {_MEMBER_WHERE}", (utc_lo, utc_hi),
    ).fetchone()
    return row["cnt"]
