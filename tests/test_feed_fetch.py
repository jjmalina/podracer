"""feed.py fetches feed bytes over HTTP with *bounded* timeouts before handing
them to feedparser.

feedparser.parse(url) does its own un-timed urllib fetch; an infinite read there
hung the single-threaded worker for ~21.5h on 2026-06-24 (it stopped draining the
job queue entirely). These tests pin the fix: a real timeout is always sent, a
transient timeout is retried then re-raised (so Worker._sync_feeds can log it and
move on), and an HTTP error surfaces instead of a silently-empty parse.
"""
import time

import httpx
import pytest

from podracer import feed


def test_fetch_feed_bytes_sends_ua_and_bounded_timeout(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["timeout"] = kwargs.get("timeout")
        captured["follow_redirects"] = kwargs.get("follow_redirects")
        return httpx.Response(200, content=b"<rss></rss>",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    body, _content_type = feed._fetch_feed_bytes("https://example.com/feed.xml")

    assert body == b"<rss></rss>"
    assert captured["headers"]["User-Agent"] == feed.USER_AGENT
    assert captured["follow_redirects"] is True
    # A real, finite timeout on both connect and read — never None (the bug was
    # an unbounded fetch).
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect is not None
    assert captured["timeout"].read is not None


def test_fetch_feed_bytes_retries_then_reraises_on_read_timeout(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)  # skip backoff waits
    calls = {"n": 0}

    def always_timeout(url, **kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", always_timeout)

    with pytest.raises(httpx.ReadTimeout):
        feed._fetch_feed_bytes("https://slow.example/feed.xml")
    assert calls["n"] == 3  # retried a few times, then gave up — didn't hang


def test_fetch_feed_bytes_raises_on_http_error(monkeypatch):
    def not_found(url, **kwargs):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", not_found)

    with pytest.raises(httpx.HTTPStatusError):
        feed._fetch_feed_bytes("https://example.com/missing.xml")


def test_configure_timeouts_overrides_defaults(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return httpx.Response(200, content=b"<rss></rss>",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(feed, "_connect_timeout", 3.0)
    monkeypatch.setattr(feed, "_read_timeout", 7.0)

    feed._fetch_feed_bytes("https://example.com/feed.xml")

    assert captured["timeout"].connect == 3.0
    assert captured["timeout"].read == 7.0


def test_fetch_episodes_parses_fetched_bytes(monkeypatch):
    """End-to-end: fetched bytes flow into feedparser and yield episodes (the
    fetch path is transparent to parsing)."""
    rss = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Show</title>
      <item>
        <title>Ep 1</title>
        <guid>ep-1</guid>
        <enclosure url="https://cdn.example/ep1.mp3" type="audio/mpeg"/>
      </item>
    </channel></rss>"""

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: httpx.Response(200, content=rss,
                                         request=httpx.Request("GET", url)),
    )

    episodes = feed.fetch_episodes("https://example.com/feed.xml")
    assert [e.title for e in episodes] == ["Ep 1"]
    assert episodes[0].audio_url == "https://cdn.example/ep1.mp3"


def test_charset_from_http_content_type_is_honored(monkeypatch):
    """A feed whose charset lives only in the HTTP Content-Type header (no
    <?xml encoding?>) must still decode correctly. feedparser can't see the
    header once it's handed bytes instead of a URL, so feed.py forwards it via
    response_headers — without that these koi8-r bytes fall back to a
    windows-1252 guess and the Cyrillic title becomes mojibake."""
    title = "Программа"
    ep_title = "Эпизод"
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>{title}</title>"
        f"<item><title>{ep_title}</title><guid>g1</guid>"
        '<enclosure url="https://cdn.example/ep.mp3" type="audio/mpeg"/></item>'
        "</channel></rss>"
    ).encode("koi8-r")

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: httpx.Response(
            200, content=body,
            headers={"content-type": "text/xml; charset=koi8-r"},
            request=httpx.Request("GET", url),
        ),
    )

    meta, episodes = feed.fetch_feed("https://example.com/feed.xml")
    assert meta.title == title
    assert [e.title for e in episodes] == [ep_title]
