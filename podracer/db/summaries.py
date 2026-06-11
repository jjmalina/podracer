import sqlite3

from podracer.models import SummaryRecord


def _from_row(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(**{k: row[k] for k in row.keys()})


def save_summary(
    conn: sqlite3.Connection, episode_id: int, data: str, model: str, backend: str,
) -> int:
    # Explicit transaction: the artifact write and the status update must
    # land together, independent of the connection's autocommit settings.
    with conn:
        row = conn.execute(
            """INSERT INTO summaries (episode_id, data, model, backend)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                 data=excluded.data, model=excluded.model,
                 backend=excluded.backend, created_at=datetime('now')
               RETURNING id""",
            (episode_id, data, model, backend),
        ).fetchone()
        conn.execute(
            "UPDATE episodes SET status = 'summarized' WHERE id = ?", (episode_id,),
        )
    return row["id"]


def get_summary(conn: sqlite3.Connection, episode_id: int) -> SummaryRecord | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return _from_row(row) if row else None


def delete_summary(conn: sqlite3.Connection, episode_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM summaries WHERE episode_id = ?", (episode_id,))
        conn.execute(
            "UPDATE episodes SET status = 'transcribed' WHERE id = ? AND status = 'summarized'",
            (episode_id,),
        )
    return cur.rowcount > 0
