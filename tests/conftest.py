"""Shared pytest fixtures."""
import sqlite3
from collections.abc import Iterator

import pytest

from podracer.db import init_db
from podracer.models import FeedEpisode


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A fresh in-memory SQLite connection with the schema applied."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    init_db(db)
    yield db
    db.close()


def feed_ep(guid: str, title: str = "t", url: str = "https://x/y.mp3") -> FeedEpisode:
    """Convenience constructor — tests rarely care about the other fields."""
    return FeedEpisode(guid=guid, title=title, audio_url=url)


def set_episode_created_at(conn: sqlite3.Connection, episode_id: int, ts: str) -> None:
    """Override an episode's created_at for deterministic time-ordering tests."""
    conn.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (ts, episode_id))
    conn.commit()


def set_podcast_subscribed_at(conn: sqlite3.Connection, podcast_id: int, ts: str) -> None:
    conn.execute("UPDATE podcasts SET subscribed_at = ? WHERE id = ?", (ts, podcast_id))
    conn.commit()
