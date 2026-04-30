import hashlib
import time

import httpx
from pydantic import BaseModel

from podracer.config import load_config

PODCAST_INDEX_BASE = "https://api.podcastindex.org/api/1.0"
TIMEOUT = 30.0


class PodcastSearchResult(BaseModel):
    id: int
    title: str
    author: str
    feed_url: str
    artwork_url: str
    description: str


def _auth_headers() -> dict[str, str]:
    cfg = load_config()
    key = cfg.podcast_index_key
    secret = cfg.podcast_index_secret
    if not key or not secret:
        raise RuntimeError(
            "Podcast Index credentials not found. Set podcast_index_key and podcast_index_secret "
            "in config.toml, .credentials/podcast_index, or env vars."
        )
    epoch = str(int(time.time()))
    auth_hash = hashlib.sha1((key + secret + epoch).encode()).hexdigest()
    return {
        "X-Auth-Key": key,
        "X-Auth-Date": epoch,
        "Authorization": auth_hash,
        "User-Agent": "podracer/0.1.0",
    }


def search_podcasts(query: str) -> list[PodcastSearchResult]:
    resp = httpx.get(
        f"{PODCAST_INDEX_BASE}/search/byterm",
        params={"q": query},
        headers=_auth_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return [
        PodcastSearchResult(
            id=f["id"],
            title=f.get("title", ""),
            author=f.get("author", ""),
            feed_url=f.get("url", ""),
            artwork_url=f.get("artwork", ""),
            description=f.get("description", ""),
        )
        for f in resp.json().get("feeds", [])
    ]
