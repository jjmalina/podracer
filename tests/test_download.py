import httpx

from podracer import download


class _FakeStream:
    """Minimal stand-in for the context manager returned by httpx.stream."""

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


def test_download_sends_user_agent(monkeypatch, tmp_path):
    """Buzzsprout and friends 403 the default httpx UA; we must send our own."""
    captured = {}

    def fake_stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeStream(b"audio-bytes")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    rel_path, size = download.download_episode(
        "https://www.buzzsprout.com/123/episodes/456-an-episode.mp3",
        str(tmp_path),
        "My Podcast",
        "An Episode",
    )

    assert captured["headers"]["User-Agent"] == download.USER_AGENT
    assert rel_path == "my-podcast/an-episode.mp3"
    assert size == len(b"audio-bytes")
