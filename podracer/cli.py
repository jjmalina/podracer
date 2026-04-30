import argparse
import json
import os
import sys

from podracer.db import (
    get_config,
    get_connection,
    get_episode,
    get_episodes,
    get_podcast,
    get_subscribed_podcasts,
    get_transcript,
    init_db,
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


def _db():
    db_path = os.environ.get("PODRACER_DB")
    conn = get_connection(db_path)
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
        print("No results found.", file=sys.stderr)
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

    print(f"Fetching feed: {feed_url}", file=sys.stderr)
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
        print(f"Podcast {args.podcast_id} not found.", file=sys.stderr)
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
    podcast = get_podcast(conn, args.podcast_id)
    if not podcast:
        print(f"Podcast {args.podcast_id} not found.", file=sys.stderr)
        sys.exit(1)

    if args.sync:
        print(f"Syncing: {podcast.title}", file=sys.stderr)
        _sync_episodes(conn, args.podcast_id, podcast.feed_url)

    db_episodes = get_episodes(conn, args.podcast_id, args.limit)

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
        print(f"Podcast not found for episode {episode.id}.", file=sys.stderr)
        return
    media_dir = get_config(conn, "media_dir") or "./data/media/"

    if episode.local_path and episode.status != "pending":
        print(f"Already downloaded: {media_dir}{episode.local_path}")
        return

    print(f"Downloading: {episode.title}", file=sys.stderr)
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
            print(f"Podcast {args.podcast_id} not found.", file=sys.stderr)
            sys.exit(1)
        episodes = get_episodes(conn, args.podcast_id, args.latest)
        if not episodes:
            print("No episodes found.", file=sys.stderr)
            sys.exit(1)
        for ep in episodes:
            _download_one(conn, ep, podcast, args.json)
        return

    if not args.episode_id:
        print("Provide an episode_id, or use --podcast and --latest.", file=sys.stderr)
        sys.exit(1)

    episode = get_episode(conn, args.episode_id)
    if not episode:
        print(f"Episode {args.episode_id} not found.", file=sys.stderr)
        sys.exit(1)
    _download_one(conn, episode, json_output=args.json)


def cmd_sync(args):
    conn = _db()

    if args.podcast_id:
        podcast = get_podcast(conn, args.podcast_id)
        if not podcast:
            print(f"Podcast {args.podcast_id} not found.", file=sys.stderr)
            sys.exit(1)
        podcasts = [podcast]
    else:
        podcasts = get_subscribed_podcasts(conn)

    if not podcasts:
        print("No subscriptions. Use `podracer subscribe <rss_url>` first.")
        return

    for podcast in podcasts:
        print(f"Syncing: {podcast.title}", file=sys.stderr)
        count = _sync_episodes(conn, podcast.id, podcast.feed_url, limit=args.limit)
        print(f"  {count} episodes", file=sys.stderr)

    print(f"Synced {len(podcasts)} podcast(s).")


def cmd_transcribe(args):
    conn = _db()
    episode = get_episode(conn, args.episode_id)
    if not episode:
        print(f"Episode {args.episode_id} not found.", file=sys.stderr)
        sys.exit(1)

    existing = get_transcript(conn, args.episode_id)
    if existing and not args.force:
        if args.json:
            print(existing.model_dump_json(indent=2))
        else:
            print(existing.text)
        return

    media_dir = get_config(conn, "media_dir") or "./data/media/"
    if not episode.local_path or episode.status == "pending":
        podcast = get_podcast(conn, episode.podcast_id)
        if not podcast:
            print(f"Podcast not found for episode {episode.id}.", file=sys.stderr)
            sys.exit(1)
        print(f"Downloading first: {episode.title}", file=sys.stderr)
        relative_path, size = download_episode(
            episode.audio_url, media_dir, podcast.title, episode.title,
        )
        update_episode_download(conn, episode.id, relative_path, size)
        audio_path = f"{media_dir}{relative_path}"
    else:
        audio_path = f"{media_dir}{episode.local_path}"

    try:
        from podracer.transcribe import load_hf_token, transcribe
    except (ImportError, AttributeError) as e:
        print(f"Error: transcription dependencies not available: {e}", file=sys.stderr)
        print("This may be due to a torch/torchaudio version conflict with vLLM.", file=sys.stderr)
        sys.exit(1)

    hf_token = None if args.no_diarize else load_hf_token()
    print(f"Transcribing: {episode.title}", file=sys.stderr)
    text = transcribe(
        audio_path,
        model_size=args.model,
        device=args.device,
        compute_type=args.compute_type,
        hf_token=hf_token,
    )

    save_transcript(conn, episode.id, text, args.model)

    if args.json:
        saved = get_transcript(conn, episode.id)
        if saved:
            print(saved.model_dump_json(indent=2))
    else:
        print(text)


def main():
    parser = argparse.ArgumentParser(prog="podracer", description="Podcast knowledge platform")
    parser.add_argument("--json", action="store_true", help="Output JSON")
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
    p_episodes.add_argument("podcast_id", type=int, help="Podcast ID")
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
    p_transcribe.add_argument("--model", default="small", help="Whisper model size (default: small)")
    p_transcribe.add_argument("--device", default="cuda", choices=["cuda", "cpu"], help="Device (default: cuda)")
    p_transcribe.add_argument("--compute-type", default="float16", help="Compute type (default: float16)")
    p_transcribe.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")
    p_transcribe.add_argument("--force", action="store_true", help="Re-transcribe even if transcript exists")
    p_transcribe.set_defaults(func=cmd_transcribe)

    p_sync = subparsers.add_parser("sync", help="Sync podcast feeds")
    p_sync.add_argument("podcast_id", type=int, nargs="?", help="Podcast ID (omit to sync all subscriptions)")
    p_sync.add_argument("--limit", type=int, default=10, help="Number of recent episodes to sync (default: 10)")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
