"""--force must actually re-fetch the audio over the network.

Regression guard for the bug where a feed served a stale enclosure at download
time (podracer cached the wrong recording) and `process --force` couldn't
correct it. The load-bearing guard is download_episode's on-disk cache check, so
these tests exercise the *real* download_episode (only httpx.stream is stubbed),
not a mock of it — an earlier version mocked download_episode wholesale and so
never caught that --force didn't reach the network.
"""
import argparse
from pathlib import Path

import httpx
import pytest

from podracer import cli, download, process
from podracer.config import Config
from podracer.db import (
    save_transcript,
    update_episode_download,
    upsert_episode,
    upsert_podcast,
)
from tests.conftest import feed_ep


class _FakeStream:
    """Stand-in for httpx.stream's context manager (see test_download.py)."""

    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=65536):
        yield self._body


def _cfg(tmp_path) -> Config:
    return Config(
        media_dir=f"{tmp_path}/",
        transcribe_backend="deepgram",
        deepgram_api_key="k",
        diarize=False,
    )


def _downloaded_episode(conn, tmp_path, audio_bytes: bytes) -> int:
    """An episode whose deterministic media path already holds `audio_bytes`."""
    pid = upsert_podcast(conn, "P", "a", "https://x/feed", None, None)
    upsert_episode(conn, pid, feed_ep("g1", url="https://x/real.m4a"))
    conn.commit()
    eid = conn.execute("SELECT id FROM episodes").fetchone()["id"]
    cached = tmp_path / "p" / "t.m4a"  # slug(P)/slug(t) + .m4a
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(audio_bytes)
    update_episode_download(conn, eid, "p/t.m4a", len(audio_bytes))  # status='downloaded'
    return eid


def test_download_episode_force_refetches(monkeypatch, tmp_path):
    """The load-bearing guard: force bypasses the full_path.exists() short-circuit."""
    streamed = {"n": 0}

    def fake_stream(method, url, **kwargs):
        streamed["n"] += 1
        return _FakeStream(b"new-audio")

    monkeypatch.setattr(httpx, "stream", fake_stream)
    cached = tmp_path / "my-podcast" / "an-episode.m4a"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old-audio")
    url = "https://h/an-episode.m4a"

    # Default: existing file returned untouched, no network call.
    _, size = download.download_episode(url, str(tmp_path), "My Podcast", "An Episode")
    assert streamed["n"] == 0
    assert cached.read_bytes() == b"old-audio" and size == len(b"old-audio")

    # force: re-fetch and overwrite.
    _, size = download.download_episode(url, str(tmp_path), "My Podcast", "An Episode", force=True)
    assert streamed["n"] == 1
    assert cached.read_bytes() == b"new-audio" and size == len(b"new-audio")


def test_transcribe_force_redownloads_wrong_cached_audio(conn, tmp_path, monkeypatch):
    """End to end: force re-fetches the audio and transcribes the fresh bytes,
    even with a stale local file AND an existing transcript present."""
    eid = _downloaded_episode(conn, tmp_path, b"STALE")
    save_transcript(conn, eid, "stale transcript", "deepgram:nova-3")
    monkeypatch.setattr(httpx, "stream", lambda method, url, **k: _FakeStream(b"FRESH"))
    # transcribe reads the file it's handed, so the transcript text reveals which
    # bytes were actually used.
    monkeypatch.setattr(process, "transcribe", lambda path, **k: Path(path).read_bytes().decode())

    process.transcribe_episode(conn, _cfg(tmp_path), eid, force=True)

    row = conn.execute("SELECT text FROM transcripts WHERE episode_id=?", (eid,)).fetchone()
    assert row["text"] == "FRESH"  # not "STALE" — proves the network re-fetch happened
    assert (tmp_path / "p" / "t.m4a").read_bytes() == b"FRESH"


def test_transcribe_no_force_reuses_cached_audio(conn, tmp_path, monkeypatch):
    """Without force, the cached audio is reused and no download is attempted."""
    eid = _downloaded_episode(conn, tmp_path, b"STALE")  # no transcript yet

    def no_network(*a, **k):
        raise AssertionError("must not re-download without force")

    monkeypatch.setattr(httpx, "stream", no_network)
    monkeypatch.setattr(process, "transcribe", lambda path, **k: Path(path).read_bytes().decode())

    process.transcribe_episode(conn, _cfg(tmp_path), eid, force=False)

    row = conn.execute("SELECT text FROM transcripts WHERE episode_id=?", (eid,)).fetchone()
    assert row["text"] == "STALE"  # cached audio reused


def test_transcribe_cli_orphaned_episode_exits_cleanly(conn, monkeypatch):
    """resolve_audio_path raising RuntimeError (e.g. a missing podcast row)
    becomes a clean error + exit 1 in cmd_transcribe, not a traceback — parity
    with cmd_process / cmd_summarize."""
    pid = upsert_podcast(conn, "P", "a", "https://x/feed", None, None)
    upsert_episode(conn, pid, feed_ep("g1"))
    conn.commit()
    eid = conn.execute("SELECT id FROM episodes").fetchone()["id"]

    def boom(*a, **k):
        raise RuntimeError("podcast 999 not found for episode 1")

    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(cli, "_config", lambda: _cfg("/x"))
    monkeypatch.setattr(cli, "resolve_audio_path", boom)

    args = argparse.Namespace(episode_id=eid, force=True, json=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_transcribe(args)
    assert exc.value.code == 1
