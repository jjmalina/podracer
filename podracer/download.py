import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Some podcast hosts (e.g. Buzzsprout) return 403 for requests with httpx's
# default User-Agent. They accept a distinct app identifier, so send one.
USER_AGENT = "podracer/0.1.0"


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:80]


def download_episode(audio_url: str, media_dir: str, podcast_title: str,
                     episode_title: str) -> tuple[str, int]:
    """Download an episode and return (relative_path, file_size_bytes)."""
    ext = Path(urlparse(audio_url).path).suffix or ".mp3"
    podcast_slug = slugify(podcast_title)
    episode_slug = slugify(episode_title)
    relative_path = f"{podcast_slug}/{episode_slug}{ext}"
    full_path = Path(media_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if full_path.exists():
        return relative_path, full_path.stat().st_size

    with httpx.stream(
        "GET", audio_url, follow_redirects=True, timeout=600.0,
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
