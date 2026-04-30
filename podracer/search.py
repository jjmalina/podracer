import hashlib
import os
import time

import httpx
from pydantic import BaseModel


class PodcastSearchResult(BaseModel):
    id: int
    title: str
    author: str
    feed_url: str
    artwork_url: str
    description: str


PODCAST_INDEX_BASE = "https://api.podcastindex.org/api/1.0"
TIMEOUT = 30.0


def _get_credentials() -> tuple[str, str]:
    key = os.environ.get("PODCAST_INDEX_KEY", "")
    secret = os.environ.get("PODCAST_INDEX_SECRET", "")
    if key and secret:
        return key, secret
    from pathlib import Path
    for base in [Path.cwd(), Path(__file__).resolve().parent.parent]:
        cred_path = base / ".credentials" / "podcast_index"
        if cred_path.exists():
            lines = cred_path.read_text().strip().splitlines()
            values = [line.split("=", 1)[-1].strip() if "=" in line else line.strip() for line in lines]
            if len(values) >= 2:
                return values[0], values[1]
    raise RuntimeError(
        "Podcast Index credentials not found. Either set PODCAST_INDEX_KEY and "
        "PODCAST_INDEX_SECRET env vars, or create .credentials/podcast_index with "
        "key on line 1 and secret on line 2."
    )


def _auth_headers() -> dict[str, str]:
    key, secret = _get_credentials()
    epoch = str(int(time.time()))
    hash_input = key + secret + epoch
    auth_hash = hashlib.sha1(hash_input.encode()).hexdigest()
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


def get_podcast_by_feed_id(feed_id: int) -> dict | None:
    resp = httpx.get(
        f"{PODCAST_INDEX_BASE}/podcasts/byfeedid",
        params={"id": feed_id},
        headers=_auth_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("feed")


def get_episodes_by_feed_id(feed_id: int, max_results: int = 20) -> list[dict]:
    resp = httpx.get(
        f"{PODCAST_INDEX_BASE}/episodes/byfeedid",
        params={"id": feed_id, "max": max_results},
        headers=_auth_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])
