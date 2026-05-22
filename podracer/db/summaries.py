import sqlite3

from podracer.models import SummaryRecord


def _from_row(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(**{k: row[k] for k in row.keys()})


def save_summary(
    conn: sqlite3.Connection, episode_id: int, data: str, model: str, backend: str,
) -> int:
    conn.execute(
        """INSERT INTO summaries (episode_id, data, model, backend)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(episode_id) DO UPDATE SET
             data=excluded.data, model=excluded.model,
             backend=excluded.backend, created_at=datetime('now')""",
        (episode_id, data, model, backend),
    )
    conn.execute(
        "UPDATE episodes SET status = 'summarized' WHERE id = ?", (episode_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM summaries WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return row["id"]


def get_summary(conn: sqlite3.Connection, episode_id: int) -> SummaryRecord | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return _from_row(row) if row else None
