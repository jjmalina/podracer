import sqlite3

from podracer.models import Transcript


def _from_row(row: sqlite3.Row) -> Transcript:
    return Transcript(**{k: row[k] for k in row.keys()})


def save_transcript(
    conn: sqlite3.Connection, episode_id: int, text: str, model: str,
    language: str | None = None,
) -> int:
    # Explicit transaction: the artifact write and the status update must
    # land together, independent of the connection's autocommit settings.
    with conn:
        row = conn.execute(
            """INSERT INTO transcripts (episode_id, text, model, language)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                 text=excluded.text, model=excluded.model,
                 language=excluded.language,
                 created_at=datetime('now')
               RETURNING id""",
            (episode_id, text, model, language),
        ).fetchone()
        conn.execute(
            "UPDATE episodes SET status = 'transcribed' WHERE id = ?", (episode_id,),
        )
    return row["id"]


def get_transcript(conn: sqlite3.Connection, episode_id: int) -> Transcript | None:
    row = conn.execute(
        "SELECT * FROM transcripts WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    return _from_row(row) if row else None


def transcript_exists(conn: sqlite3.Connection, episode_id: int) -> bool:
    """Whether a transcript row exists — without loading the (~200 KB) text."""
    row = conn.execute(
        "SELECT 1 FROM transcripts WHERE episode_id = ? LIMIT 1", (episode_id,),
    ).fetchone()
    return row is not None
