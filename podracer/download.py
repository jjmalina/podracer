import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import sentry_sdk

from podracer import USER_AGENT, logger
from podracer.db import set_podcast_artwork_path
from podracer.models import Podcast

# Image types whose extension we preserve; anything else is stored as .jpg.
ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:80]


def download_episode(audio_url: str, media_dir: str, podcast_title: str,
                     episode_title: str, *, force: bool = False) -> tuple[str, int]:
    """Download an episode and return (relative_path, file_size_bytes).

    The on-disk path is derived deterministically from the titles, so a normal
    call returns any existing file as-is. force=True re-fetches and overwrites
    it — needed to correct a bad/stale cached download (the audio the feed
    served at download time was wrong).
    """
    ext = Path(urlparse(audio_url).path).suffix or ".mp3"
    podcast_slug = slugify(podcast_title)
    episode_slug = slugify(episode_title)
    relative_path = f"{podcast_slug}/{episode_slug}{ext}"
    full_path = Path(media_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and full_path.exists():
        return relative_path, full_path.stat().st_size

    with httpx.stream(
        "GET", audio_url, follow_redirects=True,
        # Long read for big audio bodies, but a dead host fails the connect fast
        # rather than burning the full 600s.
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
        headers={"User-Agent": USER_AGENT},
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(full_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    mb_done = downloaded / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    print(
                        f"\r  {mb_done:.1f} / {mb_total:.1f} MB ({pct}%)",
                        end="", file=sys.stderr, flush=True,
                    )
        if total:
            print(file=sys.stderr)

    return relative_path, full_path.stat().st_size


def download_artwork(artwork_url: str, media_dir: str, podcast_title: str) -> str:
    """Download a podcast cover into media_dir; return its media-relative path.

    Stored next to the podcast's audio as `{slug}/cover.{ext}`, fetched with the
    app User-Agent (some hosts 403 the default one — see USER_AGENT)."""
    ext = Path(urlparse(artwork_url).path).suffix.lower()
    if ext not in ARTWORK_EXTS:
        ext = ".jpg"
    relative_path = f"{slugify(podcast_title)}/cover{ext}"
    full_path = Path(media_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    resp = httpx.get(
        artwork_url, follow_redirects=True, timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    full_path.write_bytes(resp.content)
    return relative_path


def ensure_artwork_cached(conn: sqlite3.Connection, podcast: Podcast, media_dir: str) -> bool:
    """Cache a podcast's cover locally if it isn't already; return True when a
    usable local copy exists afterwards.

    Never raises: a dead or slow image host must not break a subscribe or a feed
    sync — the caller just falls back to the generated placeholder tile."""
    if not podcast.artwork_url:
        return False
    if podcast.artwork_path and (Path(media_dir) / podcast.artwork_path).exists():
        return True
    try:
        relative_path = download_artwork(podcast.artwork_url, media_dir, podcast.title)
    except Exception:
        logger.exception("artwork_cache_failed", podcast=podcast.title)
        sentry_sdk.capture_exception()
        return False
    set_podcast_artwork_path(conn, podcast.id, relative_path)
    return True
