import html
import re
from datetime import datetime

import feedparser
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from podracer import USER_AGENT
from podracer.models import FeedEpisode, FeedMetadata

# Feed-fetch timeouts. feedparser.parse() does its own *un-timed* urllib fetch
# when handed a URL — an infinite read there hung the single-threaded worker for
# ~21.5h on 2026-06-24. So we fetch the bytes with httpx (real connect/read
# timeouts) and parse those instead. Defaults are used by CLI/one-off callers;
# the long-running entry points (worker, web server) override them from config
# via configure_timeouts() at startup.
_connect_timeout: float = 10.0
_read_timeout: float = 30.0


def configure_timeouts(connect_seconds: float, read_seconds: float) -> None:
    """Set the feed-fetch connect/read timeouts (called once at startup)."""
    global _connect_timeout, _read_timeout
    _connect_timeout = connect_seconds
    _read_timeout = read_seconds


@retry(
    retry=retry_if_exception_type(
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout),
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    reraise=True,
)
def _fetch_feed_bytes(feed_url: str) -> tuple[bytes, str | None]:
    """Fetch raw feed bytes (+ the HTTP Content-Type) with bounded timeouts.

    Follows redirects. Retries transient connect/read timeouts a few times, then
    re-raises so the caller's per-feed error handling (Worker._sync_feeds) logs
    it and moves on — rather than blocking forever on a dead/slow host. Returns
    the Content-Type header alongside the body so the parser can recover the
    charset (see _fetch_parsed_feed)."""
    timeout = httpx.Timeout(
        connect=_connect_timeout, read=_read_timeout,
        write=_connect_timeout, pool=_connect_timeout,
    )
    resp = httpx.get(
        feed_url, follow_redirects=True, timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type")


def _fetch_parsed_feed(feed_url: str):
    """Fetch a feed over HTTP and hand the bytes to feedparser.

    feedparser can't see the HTTP response once we give it bytes instead of a
    URL, so a feed that declares its charset only in the Content-Type header
    (no <?xml encoding?> declaration) would be mis-decoded into mojibake. We
    forward the header via response_headers so encoding detection matches the
    old feedparser.parse(url) behavior."""
    content, content_type = _fetch_feed_bytes(feed_url)
    response_headers = {"content-type": content_type} if content_type else {}
    return feedparser.parse(content, response_headers=response_headers)


def parse_duration(duration_str: str | None) -> int | None:
    if not duration_str:
        return None
    if duration_str.isdigit():
        return int(duration_str)
    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None
    return None


def _parse_date(entry: dict) -> str | None:
    published = entry.get("published_parsed")
    if published:
        try:
            return datetime(*published[:6]).isoformat()
        except (ValueError, TypeError):
            pass
    return None


def _get_audio_url(entry: dict) -> str | None:
    for link in entry.get("links", []):
        href = link.get("href", "")
        if link.get("rel") == "enclosure" and href:
            return href
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        if href:
            return href
    return None


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    clean = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    clean = re.sub(r"</(?:p|div|li|h[1-6]|tr|blockquote)>", "\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    # Decode entities AFTER stripping tags so &lt;script&gt; can't smuggle a tag through.
    clean = html.unescape(clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip() or None


def _get_show_notes(entry: dict) -> str | None:
    """Extract the richest available show notes from a feed entry."""
    content_list = entry.get("content", [])
    if content_list:
        longest = max(content_list, key=lambda c: len(c.get("value", "")))
        text = _strip_html(longest.get("value"))
        if text:
            return text
    for field in ("summary", "description", "subtitle"):
        text = _strip_html(entry.get(field))
        if text and len(text) > 200:
            return text
    return None


# Apple Podcasts' official category list (top-level + subcategories), as of
# 2026. feedparser flattens <itunes:category> AND <itunes:keywords> into f.tags
# with the same itunes scheme and no distinguishing marker, so we can't tell a
# real category from a keyword structurally. Matching against this canonical set
# drops the vast majority of keyword spam (arbitrary words like "hacker",
# "code"). Known limitation: a <itunes:keywords> term that happens to equal a
# real category name (e.g. "comedy", "news", "politics") still slips through and
# can over-tag a show. Matched case-insensitively; the canonical casing here is
# what we store. Re-sync from Apple's published list if categories change.
_APPLE_CATEGORIES = frozenset({
    "Arts", "Books", "Design", "Fashion & Beauty", "Food", "Performing Arts",
    "Visual Arts",
    "Business", "Careers", "Entrepreneurship", "Investing", "Management",
    "Marketing", "Non-Profit",
    "Comedy", "Comedy Interviews", "Improv", "Stand-Up",
    "Education", "Courses", "How To", "Language Learning", "Self-Improvement",
    "Fiction", "Comedy Fiction", "Drama", "Science Fiction",
    "Government",
    "History",
    "Health & Fitness", "Alternative Health", "Fitness", "Medicine",
    "Mental Health", "Nutrition", "Sexuality",
    "Kids & Family", "Education for Kids", "Parenting", "Pets & Animals",
    "Stories for Kids",
    "Leisure", "Animation & Manga", "Automotive", "Aviation", "Crafts", "Games",
    "Hobbies", "Home & Garden", "Video Games",
    "Music", "Music Commentary", "Music History", "Music Interviews",
    "News", "Business News", "Daily News", "Entertainment News",
    "News Commentary", "Politics", "Sports News", "Tech News",
    "Religion & Spirituality", "Buddhism", "Christianity", "Hinduism", "Islam",
    "Judaism", "Religion", "Spirituality",
    "Science", "Astronomy", "Chemistry", "Earth Sciences", "Life Sciences",
    "Mathematics", "Natural Sciences", "Nature", "Physics", "Social Sciences",
    "Society & Culture", "Documentary", "Personal Journals", "Philosophy",
    "Places & Travel", "Relationships",
    "Sports", "Baseball", "Basketball", "Cricket", "Fantasy Sports", "Football",
    "Golf", "Hockey", "Rugby", "Running", "Soccer", "Swimming", "Tennis",
    "Volleyball", "Wilderness", "Wrestling",
    "Technology",
    "True Crime",
    "TV & Film", "After Shows", "Film History", "Film Interviews",
    "Film Reviews", "TV Reviews",
})
_CANONICAL_CATEGORY = {c.lower(): c for c in _APPLE_CATEGORIES}


def _get_categories(f: dict) -> list[str]:
    """Genre tags from the feed's <itunes:category> tags.

    feedparser flattens categories (and any <itunes:keywords>) into f.tags as
    {term, scheme, label} dicts. We keep only terms that are real Apple Podcasts
    categories, normalise to canonical casing, dedup, and preserve feed order.
    """
    categories: list[str] = []
    seen: set[str] = set()
    for tag in f.get("tags", []) or []:
        term = (tag.get("term") or "").strip()
        canonical = _CANONICAL_CATEGORY.get(term.lower())
        if canonical and canonical not in seen:
            seen.add(canonical)
            categories.append(canonical)
    return categories


def _parse_metadata(f: dict, feed_url: str) -> FeedMetadata:
    return FeedMetadata(
        title=f.get("title", "Unknown"),
        author=f.get("author") or f.get("itunes_author"),
        description=_strip_html(f.get("summary") or f.get("subtitle")),
        artwork_url=f.get("image", {}).get("href") if isinstance(f.get("image"), dict) else None,
        feed_url=feed_url,
        categories=_get_categories(f),
    )


def _parse_episodes(feed, limit: int | None = None) -> list[FeedEpisode]:
    entries = feed.entries[:limit] if limit else feed.entries
    episodes = []
    for entry in entries:
        audio_url = _get_audio_url(entry)
        if not audio_url:
            continue
        guid = entry.get("id") or entry.get("guid") or audio_url
        episodes.append(FeedEpisode(
            guid=guid,
            title=entry.get("title", "Untitled"),
            audio_url=audio_url,
            published_at=_parse_date(entry),
            duration_seconds=parse_duration(entry.get("itunes_duration")),
            description=_strip_html(entry.get("summary") or entry.get("description")),
            show_notes=_get_show_notes(entry),
        ))
    return episodes


def fetch_feed_metadata(feed_url: str) -> FeedMetadata:
    feed = _fetch_parsed_feed(feed_url)
    return _parse_metadata(feed.feed, feed_url)


def fetch_episodes(feed_url: str, limit: int | None = None) -> list[FeedEpisode]:
    feed = _fetch_parsed_feed(feed_url)
    return _parse_episodes(feed, limit)


def fetch_feed(
    feed_url: str, limit: int | None = None,
) -> tuple[FeedMetadata, list[FeedEpisode]]:
    """Metadata + episodes from a single parse — for sync paths that need both
    (episodes to upsert, categories to (re)tag) without downloading twice."""
    feed = _fetch_parsed_feed(feed_url)
    return _parse_metadata(feed.feed, feed_url), _parse_episodes(feed, limit)
