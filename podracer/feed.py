import re
from datetime import datetime

import feedparser

from podracer.models import FeedEpisode, FeedMetadata


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
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip() or None


def fetch_feed_metadata(feed_url: str) -> FeedMetadata:
    feed = feedparser.parse(feed_url)
    f = feed.feed
    return FeedMetadata(
        title=f.get("title", "Unknown"),
        author=f.get("author") or f.get("itunes_author"),
        description=_strip_html(f.get("summary") or f.get("subtitle")),
        artwork_url=f.get("image", {}).get("href") if isinstance(f.get("image"), dict) else None,
        feed_url=feed_url,
    )


def fetch_episodes(feed_url: str, limit: int | None = None) -> list[FeedEpisode]:
    feed = feedparser.parse(feed_url)
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
        ))
    return episodes
