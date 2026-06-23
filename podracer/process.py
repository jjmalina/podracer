"""Pipeline stages used by both the CLI's `process` command and the worker.

Each stage is idempotent: skips its step if the relevant artifact already
exists (unless force=True). All three functions take an open SQLite
connection and a Config.
"""
import os
import sqlite3

import structlog

from podracer import logger
from podracer.config import Config
from podracer.db import (
    enqueue_episode_pipeline,
    get_episode,
    get_podcast,
    get_summary,
    get_transcript,
    save_summary,
    save_transcript,
    set_podcast_tags,
    update_episode_download,
    update_podcast_synced,
    upsert_episode,
)
from podracer.download import download_episode
from podracer.feed import fetch_feed
from podracer.models import FeedEpisode, FeedMetadata
from podracer.summarize import Backend, summarize
from podracer.transcribe import transcribe


def apply_feed(
    conn: sqlite3.Connection,
    podcast_id: int,
    meta: FeedMetadata,
    episodes: list[FeedEpisode],
) -> int:
    """Persist a parsed feed: upsert episodes, bump last_synced_at, refresh tags.

    The episode upserts and the last_synced_at watermark commit together (one
    transaction, rolled back on error). Topic tags are then refreshed
    best-effort — set_podcast_tags is idempotent, skips unchanged sets, and
    never wipes tags on a category-less feed. Shared by every sync path (CLI,
    web, worker) so they stay consistent. Returns the episode count.
    """
    try:
        for ep in episodes:
            upsert_episode(conn, podcast_id, ep)
        update_podcast_synced(conn, podcast_id)
    except Exception:
        conn.rollback()
        raise
    set_podcast_tags(conn, podcast_id, meta.categories)
    return len(episodes)


def sync_podcast(
    conn: sqlite3.Connection, podcast_id: int, feed_url: str, limit: int | None = None,
) -> int:
    """Fetch a feed once and apply it (episodes + last_synced + tags)."""
    meta, episodes = fetch_feed(feed_url, limit=limit)
    return apply_feed(conn, podcast_id, meta, episodes)


def _resolve_audio_path(conn: sqlite3.Connection, cfg: Config, episode) -> str:
    media_dir = cfg.media_dir
    if episode.local_path and episode.status != "pending":
        return f"{media_dir}{episode.local_path}"
    podcast = get_podcast(conn, episode.podcast_id)
    if not podcast:
        raise RuntimeError(f"podcast {episode.podcast_id} not found for episode {episode.id}")
    logger.info("Downloading: %s", episode.title)
    relative_path, size = download_episode(
        episode.audio_url, media_dir, podcast.title, episode.title,
    )
    update_episode_download(conn, episode.id, relative_path, size)
    return f"{media_dir}{relative_path}"


def transcribe_episode(
    conn: sqlite3.Connection, cfg: Config, episode_id: int, *, force: bool = False,
) -> None:
    """Ensure the episode is downloaded, then transcribe it. Idempotent."""
    episode = get_episode(conn, episode_id)
    if not episode:
        raise RuntimeError(f"episode {episode_id} not found")

    if not force and get_transcript(conn, episode_id):
        logger.info("Transcript exists for %s, skipping.", episode.title)
        return

    audio_path = _resolve_audio_path(conn, cfg, episode)

    backend = cfg.transcribe_backend
    if backend == "deepgram":
        if not cfg.deepgram_api_key:
            raise RuntimeError("deepgram backend requires DEEPGRAM_API_KEY")
        model = cfg.transcribe_deepgram_model
    elif backend == "whisperx-http":
        if not cfg.transcribe_service_url:
            raise RuntimeError("whisperx-http requires transcribe.service_url")
        model = cfg.transcribe_whisperx_model
    else:
        raise RuntimeError(f"unknown transcribe backend: {backend!r}")

    logger.info("Transcribing: %s (backend=%s)", episode.title, backend)
    text = transcribe(
        audio_path,
        backend=backend,
        model=model,
        deepgram_api_key=cfg.deepgram_api_key,
        service_url=cfg.transcribe_service_url,
        service_auth_token=cfg.transcribe_service_auth_token,
        diarize=cfg.diarize,
    )
    save_transcript(conn, episode.id, text, f"{backend}:{model}")


def _build_summarize_backend(cfg: Config, backend: str | None, model: str | None) -> Backend:
    backend_name = backend or cfg.summarize_backend
    model_name = model or cfg.summarize_model
    base_url = cfg.summarize_base_url

    if backend_name == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY") or cfg.openrouter_api_key
        if not api_key:
            raise RuntimeError("openrouter backend requires OPENROUTER_API_KEY")
        return Backend.openrouter(model_name, api_key)
    if backend_name == "vllm":
        return Backend.vllm(model_name, base_url or "http://localhost:8000")
    return Backend.ollama(model_name, base_url or "http://localhost:11434")


def summarize_episode(
    conn: sqlite3.Connection, cfg: Config, episode_id: int, *,
    force: bool = False, backend: str | None = None, model: str | None = None,
):
    """Run summarization on an existing transcript. Idempotent.

    Returns the PodcastSummary, or None if a summary already existed and
    `force` is False.
    """
    episode = get_episode(conn, episode_id)
    if not episode:
        raise RuntimeError(f"episode {episode_id} not found")

    if not force and get_summary(conn, episode_id):
        logger.info("Summary exists for %s, skipping.", episode.title)
        return None

    transcript = get_transcript(conn, episode_id)
    if not transcript:
        raise RuntimeError(f"no transcript for episode {episode_id}; transcribe first")

    sum_backend = _build_summarize_backend(cfg, backend, model)
    podcast = get_podcast(conn, episode.podcast_id)
    logger.info("Summarizing: %s", episode.title)
    # Tag every LLM event (llm_call, llm_degenerate_output, ...) with episode_id.
    # The worker also binds this per job; binding here covers the CLI path too.
    with structlog.contextvars.bound_contextvars(episode_id=episode.id):
        result = summarize(
            transcript.text, backend=sum_backend,
            show_notes=episode.show_notes,
            podcast_description=podcast.description if podcast else None,
        )
    save_summary(conn, episode.id, result.model_dump_json(),
                 sum_backend.model, sum_backend.name)
    return result


def queue_latest_unprocessed_episode(
    conn: sqlite3.Connection, cfg: Config, podcast_id: int,
) -> int | None:
    """Find the most recently published episode for a podcast that doesn't
    already have a summary or an active job, and enqueue transcribe+summarize
    jobs for it. Returns the episode_id queued, or None if everything is
    already processed / in flight.

    Used on subscribe to give the user something to listen to immediately.
    """
    row = conn.execute(
        """SELECT e.id FROM episodes e
           WHERE e.podcast_id = ?
             AND NOT EXISTS (
                 SELECT 1 FROM summaries s WHERE s.episode_id = e.id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM jobs j
                 WHERE j.episode_id = e.id
                   AND j.status IN ('queued', 'running')
             )
           ORDER BY e.published_at DESC NULLS LAST, e.id DESC
           LIMIT 1""",
        (podcast_id,),
    ).fetchone()
    if not row:
        return None
    if enqueue_episode_pipeline(conn, row["id"], max_attempts=cfg.max_attempts):
        return row["id"]
    return None


def process_episode(
    conn: sqlite3.Connection, cfg: Config, episode_id: int, *,
    force: bool = False, backend: str | None = None, model: str | None = None,
):
    """Convenience: transcribe_episode + summarize_episode."""
    transcribe_episode(conn, cfg, episode_id, force=force)
    return summarize_episode(
        conn, cfg, episode_id, force=force, backend=backend, model=model,
    )
