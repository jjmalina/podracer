import sqlite3

from podracer.models import Transcript


def _from_row(row: sqlite3.Row) -> Transcript:
    return Transcript(**{k: row[k] for k in row.keys()})


def save_transcript(
    conn: sqlite3.Connection, episode_id: int, text: str, model: str,
    language: str | None = None,
) -> int:
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
        "UPDATE episodes SET status = 'transcribed' WHERE id = ?", (episode_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM transcripts WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return row["id"]


def get_transcript(conn: sqlite3.Connection, episode_id: int) -> Transcript | None:
    row = conn.execute(
        "SELECT * FROM transcripts WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return _from_row(row) if row else None
