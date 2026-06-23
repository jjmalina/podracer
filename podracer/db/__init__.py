"""SQLite data layer for podracer.

This package is the only place that talks to the database. Imports
re-export everything so callers can keep using `from podracer.db import X`.
"""
from podracer.db.connection import (
    DEFAULT_DB_PATH,
    DEFAULT_MEDIA_DIR,
    get_config,
    get_connection,
    init_db,
    set_config,
)
from podracer.db.episodes import (
    count_recent_episodes,
    get_episode,
    get_episode_count,
    get_episodes,
    get_recent_episodes,
    update_episode_download,
    upsert_episode,
)
from podracer.db.jobs import (
    LAST_SYNC_KEY,
    WATERMARK_KEY,
    cancel_job,
    cascade_block_dependents,
    claim_next_job,
    enqueue_episode_pipeline,
    find_new_episodes,
    get_blocked_jobs,
    get_done_jobs,
    get_failed_jobs,
    get_job,
    get_job_counts,
    get_queued_jobs,
    get_running_jobs,
    get_worker_last_sync,
    get_worker_watermark,
    init_worker_watermark,
    mark_job_done,
    mark_job_failed,
    reset_running_jobs,
    retry_job,
    set_worker_last_sync,
    set_worker_watermark,
)
from podracer.db.podcasts import (
    get_all_podcasts,
    get_all_tags,
    get_podcast,
    get_subscribed_podcasts,
    set_podcast_artwork_path,
    set_podcast_tags,
    subscribe,
    unsubscribe,
    update_podcast_synced,
    upsert_podcast,
)
from podracer.db.summaries import (
    delete_summary,
    get_summary,
    save_summary,
)
from podracer.db.transcripts import (
    get_transcript,
    save_transcript,
)

__all__ = [
    # connection
    "DEFAULT_DB_PATH", "DEFAULT_MEDIA_DIR",
    "get_connection", "init_db", "get_config", "set_config",
    # podcasts
    "upsert_podcast", "subscribe", "unsubscribe", "get_podcast",
    "get_subscribed_podcasts", "get_all_podcasts", "update_podcast_synced",
    "set_podcast_artwork_path", "set_podcast_tags", "get_all_tags",
    # episodes
    "upsert_episode", "get_episode", "get_episodes", "get_episode_count",
    "update_episode_download", "get_recent_episodes", "count_recent_episodes",
    # transcripts
    "save_transcript", "get_transcript",
    # summaries
    "save_summary", "get_summary", "delete_summary",
    # jobs / watermark
    "WATERMARK_KEY", "LAST_SYNC_KEY",
    "init_worker_watermark", "get_worker_watermark", "set_worker_watermark",
    "get_worker_last_sync", "set_worker_last_sync",
    "enqueue_episode_pipeline", "find_new_episodes",
    "claim_next_job", "mark_job_done", "mark_job_failed",
    "cascade_block_dependents", "reset_running_jobs",
    "get_job_counts", "get_running_jobs", "get_queued_jobs",
    "get_done_jobs", "get_failed_jobs", "get_blocked_jobs",
    "get_job", "retry_job", "cancel_job",
]
