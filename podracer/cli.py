import argparse
import json
import logging
import socket
import sys

import uvicorn

from podracer import logger
from podracer.config import Config, load_config
from podracer.db import (
    get_all_podcasts,
    get_connection,
    get_episode,
    get_episodes,
    get_failed_jobs,
    get_job_counts,
    get_podcast,
    get_running_jobs,
    get_subscribed_podcasts,
    get_summary,
    get_transcript,
    get_worker_last_sync,
    get_worker_watermark,
    init_db,
    save_transcript,
    set_podcast_tags,
    subscribe,
    unsubscribe,
    update_episode_download,
    upsert_podcast,
)
from podracer.download import download_episode, ensure_artwork_cached
from podracer.feed import configure_timeouts, fetch_feed, fetch_feed_metadata
from podracer.logging_config import configure_logging
from podracer.process import (
    apply_feed,
    process_episode,
    queue_latest_unprocessed_episode,
    resolve_audio_path,
    summarize_episode,
    sync_podcast,
)
from podracer.search import search_podcasts
from podracer.sentry_config import configure_sentry
from podracer.summarize import PodcastSummary
from podracer.summarize_cli import print_summary
from podracer.transcribe import transcribe
from podracer.web.app import create_app
from podracer.worker import Worker

_cfg: Config | None = None


def _config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
        # Re-apply logging + Sentry now that config.toml is loaded (the early
        # calls in main() only saw env). env still wins over the file values.
        configure_logging(_cfg.log_format)
        configure_sentry(_cfg.sentry_dsn)
    return _cfg


def _db():
    conn = get_connection(_config().db_path)
    init_db(conn)
    return conn


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def cmd_search(args):
    results = search_podcasts(args.query)
    if not results:
        logger.info("No results found.")
        return

    if args.json:
        print(json.dumps([r.model_dump() for r in results], indent=2))
        return

    for r in results:
        print(f"\n  {r.title}")
        print(f"    Author: {r.author}")
        print(f"    Feed:   {r.feed_url}")
    print(f"\n  {len(results)} results. Subscribe with: podracer subscribe <feed_url>")


def cmd_subscribe(args):
    conn = _db()
    feed_url = args.feed_url

    logger.info("Fetching feed: %s", feed_url)
    # Single parse: metadata for the podcast row, plus episodes + categories.
    meta, episodes = fetch_feed(feed_url, limit=args.limit)
    podcast_id = upsert_podcast(conn, meta.title, meta.author, feed_url,
                                meta.artwork_url, meta.description)
    subscribe(conn, podcast_id)

    # apply_feed upserts episodes and (re)applies topic tags from the categories.
    count = apply_feed(conn, podcast_id, meta, episodes)

    queued_episode_id = None
    if not args.no_queue:
        queued_episode_id = queue_latest_unprocessed_episode(conn, _config(), podcast_id)

    if args.json:
        print(json.dumps({"id": podcast_id, "title": meta.title,
                          "episodes": count, "queued_episode_id": queued_episode_id}))
    else:
        msg = f"Subscribed to: {meta.title} ({count} episodes synced)"
        if queued_episode_id:
            msg += f"; queued episode {queued_episode_id} for processing"
        print(msg)


def cmd_unsubscribe(args):
    conn = _db()
    podcast = get_podcast(conn, args.podcast_id)
    if not podcast:
        logger.error("Podcast %s not found.", args.podcast_id)
        sys.exit(1)
    unsubscribe(conn, args.podcast_id)
    print(f"Unsubscribed from: {podcast.title}")


def cmd_list(args):
    conn = _db()
    podcasts = get_subscribed_podcasts(conn)

    if args.json:
        print(json.dumps([p.model_dump() for p in podcasts], indent=2))
        return

    if not podcasts:
        print("No subscriptions. Use `podracer subscribe <rss_url>` to add one.")
        return

    print(f"\n  {'ID':<5} {'Title':<45} {'Last Synced'}")
    print(f"  {'─' * 5} {'─' * 45} {'─' * 20}")
    for p in podcasts:
        synced = (p.last_synced_at or "never")[:19]
        print(f"  {p.id:<5} {p.title[:45]:<45} {synced}")


def cmd_episodes(args):
    conn = _db()

    if args.feed:
        meta, episodes = fetch_feed(args.feed, limit=args.limit)
        podcast_id = upsert_podcast(conn, meta.title, meta.author, args.feed,
                                    meta.artwork_url, meta.description)
        apply_feed(conn, podcast_id, meta, episodes)
        podcast = get_podcast(conn, podcast_id)
        if not podcast:
            logger.error("Failed to create podcast.")
            sys.exit(1)
    elif args.podcast_id:
        podcast = get_podcast(conn, args.podcast_id)
        if not podcast:
            logger.error("Podcast %s not found.", args.podcast_id)
            sys.exit(1)
        if args.sync:
            logger.info("Syncing: %s", podcast.title)
            sync_podcast(conn, args.podcast_id, podcast.feed_url)
    else:
        logger.error("Provide a podcast_id or --feed <url>.")
        sys.exit(1)

    db_episodes = get_episodes(conn, podcast.id, args.limit)

    if args.search:
        term = args.search.lower()
        db_episodes = [ep for ep in db_episodes if term in ep.title.lower()]

    if args.json:
        print(json.dumps([e.model_dump() for e in db_episodes], indent=2))
        return

    print(f"\n  {podcast.title} — {podcast.author or ''}")
    print(f"  {'─' * 100}")
    print(f"  {'ID':<5} {'Published':<12} {'Duration':<10} {'Status':<10} Title")
    print(f"  {'─' * 5} {'─' * 12} {'─' * 10} {'─' * 10} {'─' * 60}")
    for ep in db_episodes:
        pub = (ep.published_at or "")[:10]
        dur = _format_duration(ep.duration_seconds)
        print(f"  {ep.id:<5} {pub:<12} {dur:<10} {ep.status:<10} {ep.title}")


def _download_one(conn, episode, podcast=None, json_output=False):
    if not podcast:
        podcast = get_podcast(conn, episode.podcast_id)
    if not podcast:
        logger.error("Podcast not found for episode %s.", episode.id)
        return
    media_dir = _config().media_dir

    if episode.local_path and episode.status != "pending":
        print(f"Already downloaded: {media_dir}{episode.local_path}")
        return

    logger.info("Downloading: %s", episode.title)
    relative_path, size = download_episode(
        episode.audio_url, media_dir, podcast.title, episode.title,
    )
    update_episode_download(conn, episode.id, relative_path, size)

    if json_output:
        print(json.dumps({"episode_id": episode.id, "path": f"{media_dir}{relative_path}", "size_bytes": size}))
    else:
        print(f"Saved to: {media_dir}{relative_path}")


def cmd_download(args):
    conn = _db()

    if args.podcast_id and args.latest:
        podcast = get_podcast(conn, args.podcast_id)
        if not podcast:
            logger.error("Podcast %s not found.", args.podcast_id)
            sys.exit(1)
        episodes = get_episodes(conn, args.podcast_id, args.latest)
        if not episodes:
            logger.error("No episodes found.")
            sys.exit(1)
        for ep in episodes:
            _download_one(conn, ep, podcast, args.json)
        return

    if not args.episode_id:
        logger.error("Provide an episode_id, or use --podcast and --latest.")
        sys.exit(1)

    episode = get_episode(conn, args.episode_id)
    if not episode:
        logger.error("Episode %s not found.", args.episode_id)
        sys.exit(1)
    _download_one(conn, episode, json_output=args.json)


def cmd_sync(args):
    conn = _db()

    if args.podcast_id:
        podcast = get_podcast(conn, args.podcast_id)
        if not podcast:
            logger.error("Podcast %s not found.", args.podcast_id)
            sys.exit(1)
        podcasts = [podcast]
    else:
        podcasts = get_all_podcasts(conn)

    if not podcasts:
        print("No podcasts found. Use `podracer subscribe <rss_url>` to add one.")
        return

    for podcast in podcasts:
        logger.info("Syncing: %s", podcast.title)
        count = sync_podcast(conn, podcast.id, podcast.feed_url, limit=args.limit)
        logger.info("  %d episodes", count)

    print(f"Synced {len(podcasts)} podcast(s).")


def cmd_transcribe(args):
    conn = _db()
    episode = get_episode(conn, args.episode_id)
    if not episode:
        logger.error("Episode %s not found.", args.episode_id)
        sys.exit(1)

    existing = get_transcript(conn, args.episode_id)
    if existing and not args.force:
        if args.json:
            print(existing.model_dump_json(indent=2))
        else:
            print(existing.text)
        return

    cfg = _config()
    # Shared download path (honours --force to re-fetch a bad cached download).
    try:
        audio_path = resolve_audio_path(conn, cfg, episode, force=args.force)
    except RuntimeError as e:  # e.g. orphaned episode (podcast row missing)
        logger.error("%s", e)
        sys.exit(1)

    backend = args.backend or cfg.transcribe_backend
    model = args.model or cfg.transcribe_deepgram_model
    diarize = not args.no_diarize

    if backend == "deepgram" and not cfg.deepgram_api_key:
        logger.error("Deepgram backend requires DEEPGRAM_API_KEY (config, credentials, or env).")
        sys.exit(1)
    if backend == "whisperx-http" and not cfg.transcribe_service_url:
        logger.error("whisperx-http backend requires transcribe.service_url in config.")
        sys.exit(1)

    logger.info("Transcribing: %s (backend=%s)", episode.title, backend)
    text = transcribe(
        audio_path,
        backend=backend,
        model=model,
        deepgram_api_key=cfg.deepgram_api_key,
        service_url=cfg.transcribe_service_url,
        service_auth_token=cfg.transcribe_service_auth_token,
        diarize=diarize,
    )

    model_tag = model if backend == "deepgram" else cfg.transcribe_whisperx_model
    save_transcript(conn, episode.id, text, f"{backend}:{model_tag}")

    if args.json:
        saved = get_transcript(conn, episode.id)
        if saved:
            print(saved.model_dump_json(indent=2))
    else:
        print(text)


def cmd_summarize(args):
    conn = _db()
    episode = get_episode(conn, args.episode_id)
    if not episode:
        logger.error("Episode %s not found.", args.episode_id)
        sys.exit(1)

    existing = get_summary(conn, args.episode_id)
    if existing and not args.force:
        if args.json:
            print(existing.data)
        else:
            print_summary(PodcastSummary.model_validate_json(existing.data))
        return

    try:
        result = summarize_episode(
            conn, _config(), args.episode_id,
            force=args.force, backend=args.backend, model=args.model,
        )
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)

    if result is None:
        return

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print_summary(result)


def cmd_serve(args):
    # log_config=None: don't let uvicorn install its own logging config, so its
    # access/error loggers propagate to the root handler set up by
    # configure_logging() (in the app lifespan) and render in the same
    # console/JSON format as the rest of podracer.
    if args.reload:
        uvicorn.run("podracer.web.app:app", host=args.host, port=args.port,
                    reload=True, reload_dirs=["podracer"], log_config=None)
    else:
        cfg = _config()
        app = create_app(cfg)
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)


def cmd_process(args):
    conn = _db()
    episode = get_episode(conn, args.episode_id)
    if not episode:
        logger.error("Episode %s not found.", args.episode_id)
        sys.exit(1)

    try:
        result = process_episode(
            conn, _config(), args.episode_id,
            force=args.force, backend=args.backend, model=args.model,
        )
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)

    if result is None:
        # Skipped both stages because artifacts existed and --force wasn't passed.
        existing = get_summary(conn, args.episode_id)
        if existing:
            result = PodcastSummary.model_validate_json(existing.data)
        else:
            return

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print_summary(result)


def cmd_worker(args):
    cfg = _config()
    configure_timeouts(cfg.feed_connect_timeout_seconds, cfg.feed_read_timeout_seconds)
    # Last-resort cap for any un-timed blocking socket I/O a dependency might do;
    # real per-call timeouts (httpx, feed fetch) are tighter. Prevents a repeat
    # of the worker hanging indefinitely on a network read.
    socket.setdefaulttimeout(120)
    conn = _db()
    worker = Worker(conn, cfg)
    if args.once:
        worker.run_once()
        return
    worker.install_signal_handlers()
    logger.info("worker_starting", sync_interval_minutes=cfg.sync_interval_minutes,
                max_attempts=cfg.max_attempts)
    worker.run_forever()
    logger.info("worker_stopped")


def cmd_status(args):
    conn = _db()
    cfg = _config()
    counts = get_job_counts(conn)
    last_sync = get_worker_last_sync(conn)
    watermark = get_worker_watermark(conn)
    running = get_running_jobs(conn)
    failed = get_failed_jobs(conn, limit=5)
    podcasts = len(get_subscribed_podcasts(conn))

    if args.json:
        payload = {
            "worker": {
                "last_sync": last_sync,
                "watermark": watermark,
                "sync_interval_minutes": cfg.sync_interval_minutes,
            },
            "jobs": counts,
            "running": [j.model_dump() for j in running],
            "failed": [j.model_dump() for j in failed],
            "subscribed_podcasts": podcasts,
        }
        print(json.dumps(payload, indent=2))
        return

    print("Worker:")
    print(f"  Last sync:      {last_sync or 'never'}")
    print(f"  Watermark:      {watermark or 'unset'}")
    print(f"  Sync interval:  {cfg.sync_interval_minutes} min")
    print()
    print("Jobs:")
    for status_name in ("queued", "running", "done", "failed", "blocked"):
        print(f"  {status_name:<10} {counts[status_name]}")
    if running:
        print()
        print("Running:")
        for j in running:
            print(f"  job {j.id}: episode {j.episode_id} {j.kind} (started {j.started_at})")
    if failed:
        print()
        print("Recent failures:")
        for j in failed:
            err = (j.last_error or "")[:80]
            print(f"  job {j.id}: episode {j.episode_id} {j.kind} — {err}")
    print()
    print(f"Subscribed podcasts: {podcasts}")


def cmd_backfill_artwork(args):
    conn = _db()
    cfg = _config()
    cached = skipped = failed = 0
    for p in get_subscribed_podcasts(conn):
        # Recover a missing artwork_url from the feed (older subscriptions, or a
        # feed that didn't expose <image> when first subscribed).
        if not p.artwork_url:
            try:
                meta = fetch_feed_metadata(p.feed_url)
            except Exception:
                logger.exception("artwork_backfill_meta_failed", podcast=p.title)
                meta = None
            if meta and meta.artwork_url:
                upsert_podcast(conn, p.title, p.author, p.feed_url,
                               meta.artwork_url, p.description)
                p = get_podcast(conn, p.id)
        if not p or not p.artwork_url:
            skipped += 1
            continue
        if ensure_artwork_cached(conn, p, cfg.media_dir):
            cached += 1
        else:
            failed += 1

    if args.json:
        print(json.dumps({"cached": cached, "skipped": skipped, "failed": failed}))
    else:
        print(f"Artwork backfill: {cached} cached, {skipped} skipped (no art), {failed} failed.")


def cmd_backfill_topics(args):
    conn = _db()
    tagged = skipped = failed = 0
    for p in get_subscribed_podcasts(conn):
        try:
            meta = fetch_feed_metadata(p.feed_url)
        except Exception:
            logger.exception("topics_backfill_meta_failed", podcast=p.title)
            failed += 1
            continue
        if not meta.categories:
            skipped += 1
            continue
        set_podcast_tags(conn, p.id, meta.categories)
        tagged += 1
        if not args.json:
            print(f"  {p.title}: {', '.join(meta.categories)}")

    if args.json:
        print(json.dumps({"tagged": tagged, "skipped": skipped, "failed": failed}))
    else:
        print(f"Topics backfill: {tagged} tagged, {skipped} skipped (no categories), {failed} failed.")


def main():
    configure_logging()
    configure_sentry()

    parser = argparse.ArgumentParser(prog="podracer", description="Podcast knowledge platform")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_search = subparsers.add_parser("search", help="Search for podcasts")
    p_search.add_argument("query", help="Search query")
    p_search.set_defaults(func=cmd_search)

    p_subscribe = subparsers.add_parser("subscribe", help="Subscribe to a podcast via RSS URL")
    p_subscribe.add_argument("feed_url", help="RSS feed URL")
    p_subscribe.add_argument("--limit", type=int, default=10, help="Number of recent episodes to sync (default: 10)")
    p_subscribe.add_argument("--no-queue", action="store_true",
                             help="Don't auto-queue the latest episode for processing")
    p_subscribe.set_defaults(func=cmd_subscribe)

    p_unsubscribe = subparsers.add_parser("unsubscribe", help="Unsubscribe from a podcast")
    p_unsubscribe.add_argument("podcast_id", type=int, help="Podcast ID")
    p_unsubscribe.set_defaults(func=cmd_unsubscribe)

    p_list = subparsers.add_parser("list", help="List subscribed podcasts")
    p_list.set_defaults(func=cmd_list)

    p_episodes = subparsers.add_parser("episodes", help="List episodes for a podcast")
    p_episodes.add_argument("podcast_id", type=int, nargs="?", help="Podcast ID")
    p_episodes.add_argument("--feed", help="RSS feed URL (imports podcast without subscribing)")
    p_episodes.add_argument("--search", help="Filter episodes by title substring")
    p_episodes.add_argument("--limit", type=int, default=20, help="Max episodes to show")
    p_episodes.add_argument("--sync", action="store_true", help="Sync feed before listing")
    p_episodes.set_defaults(func=cmd_episodes)

    p_download = subparsers.add_parser("download", help="Download an episode")
    p_download.add_argument("episode_id", type=int, nargs="?", help="Episode ID")
    p_download.add_argument("--podcast", dest="podcast_id", type=int, help="Podcast ID (use with --latest)")
    p_download.add_argument("--latest", type=int, help="Download N most recent episodes")
    p_download.set_defaults(func=cmd_download)

    p_transcribe = subparsers.add_parser("transcribe", help="Transcribe an episode")
    p_transcribe.add_argument("episode_id", type=int, help="Episode ID")
    p_transcribe.add_argument("--backend", choices=["deepgram", "whisperx-http"], default=None,
                              help="Transcription backend (default: from config)")
    p_transcribe.add_argument("--model", default=None, help="Deepgram model name (default: from config)")
    p_transcribe.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")
    p_transcribe.add_argument("--force", action="store_true",
                              help="Re-download and re-transcribe even if a transcript exists")
    p_transcribe.set_defaults(func=cmd_transcribe)

    p_summarize = subparsers.add_parser("summarize", help="Summarize an episode")
    p_summarize.add_argument("episode_id", type=int, help="Episode ID")
    p_summarize.add_argument("--model", default=None, help="Model name (default: from config)")
    p_summarize.add_argument("--backend", choices=["ollama", "vllm", "openrouter"], default=None,
                             help="Inference backend (default: from config)")
    p_summarize.add_argument("--base-url", default=None, help="Backend API base URL")
    p_summarize.add_argument("--force", action="store_true", help="Re-summarize even if summary exists")
    p_summarize.set_defaults(func=cmd_summarize)

    p_process = subparsers.add_parser("process", help="Process an episode: download, transcribe, summarize")
    p_process.add_argument("episode_id", type=int, help="Episode ID")
    p_process.add_argument("--model", default=None, help="Summarization model name")
    p_process.add_argument("--backend", choices=["ollama", "vllm", "openrouter"], default=None,
                           help="Inference backend")
    p_process.add_argument("--base-url", default=None, help="Backend API base URL")
    p_process.add_argument("--force", action="store_true",
                           help="Redo download, transcription, and summarization")
    p_process.set_defaults(func=cmd_process)

    p_serve = subparsers.add_parser("serve", help="Start the web UI")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    p_serve.set_defaults(func=cmd_serve)

    p_sync = subparsers.add_parser("sync", help="Sync podcast feeds")
    p_sync.add_argument("podcast_id", type=int, nargs="?", help="Podcast ID (omit to sync all subscriptions)")
    p_sync.add_argument("--limit", type=int, default=10, help="Number of recent episodes to sync (default: 10)")
    p_sync.set_defaults(func=cmd_sync)

    p_worker = subparsers.add_parser("worker", help="Run the sync + processing worker")
    p_worker.add_argument("--once", action="store_true",
                          help="Run a single iteration and exit (for testing)")
    p_worker.set_defaults(func=cmd_worker)

    p_status = subparsers.add_parser("status", help="Show queue state for ops/debugging")
    p_status.set_defaults(func=cmd_status)

    p_backfill_artwork = subparsers.add_parser(
        "backfill-artwork", help="Cache cover art for subscribed podcasts")
    p_backfill_artwork.set_defaults(func=cmd_backfill_artwork)

    p_backfill_topics = subparsers.add_parser(
        "backfill-topics", help="Import topic/genre tags from feeds for subscribed podcasts")
    p_backfill_topics.set_defaults(func=cmd_backfill_topics)

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger("podracer").setLevel(logging.DEBUG)
    args.func(args)


if __name__ == "__main__":
    main()
