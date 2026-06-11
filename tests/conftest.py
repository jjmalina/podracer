"""Shared pytest fixtures."""
import logging
import sqlite3
from collections.abc import Iterator

import pytest
import structlog

from podracer import logging_config
from podracer.db import init_db
from podracer.models import FeedEpisode


@pytest.fixture(autouse=True)
def _isolate_logging_state() -> Iterator[None]:
    """Keep logging tests hermetic: configure_logging() swaps the root handler
    (some tests point it at a StringIO) and sets a module-global. Snapshot and
    restore both so tests don't leak handlers/format into each other."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_format = logging_config._configured_format
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging_config._configured_format = saved_format
        structlog.contextvars.clear_contextvars()


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
