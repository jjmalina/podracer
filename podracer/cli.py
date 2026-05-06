import argparse
import json
import sys

from podracer import logger
from podracer.config import Config, load_config
from podracer.db import (
    get_connection,
    get_episode,
    get_episodes,
    get_podcast,
    get_subscribed_podcasts,
    get_summary,
    get_transcript,
    init_db,
    save_summary,
    save_transcript,
    subscribe,
    unsubscribe,
    update_episode_download,
    update_podcast_synced,
    upsert_episode,
    upsert_podcast,
)
from podracer.download import download_episode
from podracer.feed import fetch_episodes, fetch_feed_metadata
from podracer.search import search_podcasts

_cfg: Config | None = None


def _config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
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


def _sync_episodes(conn, podcast_id: int, feed_url: str, limit: int | None = None) -> int:
    episodes = fetch_episodes(feed_url, limit=limit)
    for ep in episodes:
        upsert_episode(conn, podcast_id, ep)
    conn.commit()
    update_podcast_synced(conn, podcast_id)
    return len(episodes)


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
    meta = fetch_feed_metadata(feed_url)
    podcast_id = upsert_podcast(conn, meta.title, meta.author, feed_url,
                                meta.artwork_url, meta.description)
    subscribe(conn, podcast_id)

    count = _sync_episodes(conn, podcast_id, feed_url, limit=args.limit)

    if args.json:
        print(json.dumps({"id": podcast_id, "title": meta.title, "episodes": count}))
    else:
        print(f"Subscribed to: {meta.title} ({count} episodes synced)")


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
    print(f"  {'─'*5} {'─'*45} {'─'*20}")
    for p in podcasts:
        synced = (p.last_synced_at or "never")[:19]
        print(f"  {p.id:<5} {p.title[:45]:<45} {synced}")


def cmd_episodes(args):
    conn = _db()

    if args.feed:
        meta = fetch_feed_metadata(args.feed)
        podcast_id = upsert_podcast(conn, meta.title, meta.author, args.feed,
                                    meta.artwork_url, meta.description)
        conn.commit()
        _sync_episodes(conn, podcast_id, args.feed, limit=args.limit)
        podcast = get_podcast(conn, podcast_id)
    elif args.podcast_id:
        podcast = get_podcast(conn, args.podcast_id)
        if not podcast:
            logger.error("Podcast %s not found.", args.podcast_id)
            sys.exit(1)
        if args.sync:
            logger.info("Syncing: %s", podcast.title)
            _sync_episodes(conn, args.podcast_id, podcast.feed_url)
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
    print(f"  {'─' * 60}")
    print(f"  {'ID':<5} {'Published':<12} {'Duration':<10} {'Title':<40} {'Status'}")
    print(f"  {'─'*5} {'─'*12} {'─'*10} {'─'*40} {'─'*10}")
    for ep in db_episodes:
        pub = (ep.published_at or "")[:10]
        dur = _format_duration(ep.duration_seconds)
        print(f"  {ep.id:<5} {pub:<12} {dur:<10} {ep.title[:40]:<40} {ep.status}")


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
        podcasts = get_subscribed_podcasts(conn)

    if not podcasts:
        print("No subscriptions. Use `podracer subscribe <rss_url>` first.")
        return

    for podcast in podcasts:
        logger.info("Syncing: %s", podcast.title)
        count = _sync_episodes(conn, podcast.id, podcast.feed_url, limit=args.limit)
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

    media_dir = _config().media_dir
    if not episode.local_path or episode.status == "pending":
        podcast = get_podcast(conn, episode.podcast_id)
        if not podcast:
            logger.error("Podcast not found for episode %s.", episode.id)
            sys.exit(1)
        logger.info("Downloading first: %s", episode.title)
        relative_path, size = download_episode(
            episode.audio_url, media_dir, podcast.title, episode.title,
        )
        update_episode_download(conn, episode.id, relative_path, size)
        audio_path = f"{media_dir}{relative_path}"
    else:
        audio_path = f"{media_dir}{episode.local_path}"

    try:
        from podracer.transcribe import transcribe
    except (ImportError, AttributeError) as e:
        logger.error("Transcription dependencies not available: %s", e)
        logger.error("This may be due to a torch/torchaudio version conflict with vLLM.")
        sys.exit(1)

    cfg = _config()
    hf_token = None if args.no_diarize else cfg.hf_token
    model = args.model or cfg.transcribe_model
    device = args.device or cfg.transcribe_device
    compute_type = args.compute_type or cfg.transcribe_compute_type

    logger.info("Transcribing: %s", episode.title)
    text = transcribe(
        audio_path,
        model_size=model,
        device=device,
        compute_type=compute_type,
        hf_token=hf_token,
    )

    save_transcript(conn, episode.id, text, model)

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
            from podracer.summarize import PodcastSummary
            from podracer.summarize_cli import print_summary

            result = PodcastSummary.model_validate_json(existing.data)
            print_summary(result)
        return

    transcript = get_transcript(conn, args.episode_id)
    if not transcript:
        logger.error("No transcript for episode %s. Run `podracer transcribe %s` first.",
                      args.episode_id, args.episode_id)
        sys.exit(1)

    from podracer.summarize import Backend, summarize

    cfg = _config()
    backend_name = args.backend or cfg.summarize_backend
    model = args.model or cfg.summarize_model
    base_url = args.base_url or cfg.summarize_base_url

    if backend_name == "openrouter":
        api_key = cfg.openrouter_api_key
        if not api_key:
            logger.error("OpenRouter API key not configured. Set in config.toml, .credentials/, or env var.")
            sys.exit(1)
        backend = Backend.openrouter(model, api_key)
    elif backend_name == "vllm":
        backend = Backend.vllm(model, base_url or "http://localhost:8000")
    else:
        backend = Backend.ollama(model, base_url or "http://localhost:11434")

    logger.info("Summarizing: %s", episode.title)
    result = summarize(transcript.text, backend=backend)

    save_summary(conn, episode.id, result.model_dump_json(), model, backend_name)

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        from podracer.summarize_cli import print_summary

        print_summary(result)


def cmd_process(args):
    conn = _db()
    episode = get_episode(conn, args.episode_id)
    if not episode:
        logger.error("Episode %s not found.", args.episode_id)
        sys.exit(1)

    podcast = get_podcast(conn, episode.podcast_id)
    if not podcast:
        logger.error("Podcast not found for episode %s.", episode.id)
        sys.exit(1)

    # Download
    media_dir = _config().media_dir
    if not episode.local_path or episode.status == "pending":
        logger.info("Downloading: %s", episode.title)
        relative_path, size = download_episode(
            episode.audio_url, media_dir, podcast.title, episode.title,
        )
        update_episode_download(conn, episode.id, relative_path, size)
        audio_path = f"{media_dir}{relative_path}"
    else:
        audio_path = f"{media_dir}{episode.local_path}"
        logger.info("Already downloaded: %s", audio_path)

    # Transcribe
    existing_transcript = get_transcript(conn, args.episode_id)
    if existing_transcript and not args.force:
        logger.info("Transcript exists, skipping. Use --force to redo.")
        transcript_text = existing_transcript.text
    else:
        try:
            from podracer.transcribe import transcribe
        except (ImportError, AttributeError) as e:
            logger.error("Transcription dependencies not available: %s", e)
            sys.exit(1)

        cfg = _config()
        hf_token = cfg.hf_token
        model = cfg.transcribe_model
        device = cfg.transcribe_device
        compute_type = cfg.transcribe_compute_type

        logger.info("Transcribing: %s", episode.title)
        transcript_text = transcribe(
            audio_path,
            model_size=model,
            device=device,
            compute_type=compute_type,
            hf_token=hf_token,
        )
        save_transcript(conn, episode.id, transcript_text, model)

    # Summarize
    existing_summary = get_summary(conn, args.episode_id)
    if existing_summary and not args.force:
        logger.info("Summary exists, skipping. Use --force to redo.")
        from podracer.summarize import PodcastSummary
        result = PodcastSummary.model_validate_json(existing_summary.data)
    else:
        from podracer.summarize import Backend, summarize

        cfg = _config()
        backend_name = args.backend or cfg.summarize_backend
        model_name = args.model or cfg.summarize_model
        base_url = args.base_url or cfg.summarize_base_url

        if backend_name == "openrouter":
            import os
            api_key = os.environ.get("OPENROUTER_API_KEY") or cfg.openrouter_api_key
            if not api_key:
                logger.error("OPENROUTER_API_KEY not set.")
                sys.exit(1)
            backend = Backend.openrouter(model_name, api_key)
        elif backend_name == "vllm":
            backend = Backend.vllm(model_name, base_url or "http://localhost:8000")
        else:
            backend = Backend.ollama(model_name, base_url or "http://localhost:11434")

        logger.info("Summarizing: %s", episode.title)
        result = summarize(transcript_text, backend=backend)
        save_summary(conn, episode.id, result.model_dump_json(), model_name, backend_name)

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        from podracer.summarize_cli import print_summary
        print_summary(result)


def main():
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
    p_transcribe.add_argument("--model", default=None, help="Whisper model size (default: from config)")
    p_transcribe.add_argument("--device", default=None, help="Device: cuda or cpu (default: from config)")
    p_transcribe.add_argument("--compute-type", default=None, help="Compute type (default: from config)")
    p_transcribe.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")
    p_transcribe.add_argument("--force", action="store_true", help="Re-transcribe even if transcript exists")
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
    p_process.add_argument("--force", action="store_true", help="Redo transcription and summarization")
    p_process.set_defaults(func=cmd_process)

    p_sync = subparsers.add_parser("sync", help="Sync podcast feeds")
    p_sync.add_argument("podcast_id", type=int, nargs="?", help="Podcast ID (omit to sync all subscriptions)")
    p_sync.add_argument("--limit", type=int, default=10, help="Number of recent episodes to sync (default: 10)")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger("podracer").setLevel(logging.DEBUG)
    args.func(args)


if __name__ == "__main__":
    main()
