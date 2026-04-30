# Phase 1a: Podcast Search & Download CLI

## Goal

Search for podcasts, browse episodes, download audio files, and manage subscriptions — all via CLI, backed by SQLite.

## API: Podcast Index

- Free API at [podcastindex.org](https://podcastindex.org/) — requires signup for API key + secret
- Auth: API key + secret used to generate an Authorization header (HMAC-SHA1)
- Endpoints we need:
  - `GET /api/1.0/search/byterm?q=<query>` — search podcasts by name
  - `GET /api/1.0/podcasts/byfeedid?id=<id>` — get podcast details
  - `GET /api/1.0/episodes/byfeedid?id=<id>` — list episodes (paginated)
- Rate limits: 300 requests/minute (generous for CLI use)
- Returns JSON with podcast metadata, feed URLs, episode lists

## RSS Feed Parsing

For episode details (audio URLs, descriptions), parse the RSS feed directly using `feedparser`:
- Podcast Index gives us the `feedUrl`
- RSS `<enclosure>` tags contain the audio file URL
- More reliable than depending solely on the API for episode audio URLs

## Data Model (SQLite)

```sql
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE podcasts (
    id INTEGER PRIMARY KEY,             -- Podcast Index feed ID
    title TEXT NOT NULL,
    author TEXT,
    feed_url TEXT NOT NULL,
    artwork_url TEXT,
    description TEXT,
    subscribed INTEGER NOT NULL DEFAULT 0,  -- boolean
    last_synced_at TEXT,                    -- ISO 8601
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id),
    guid TEXT NOT NULL,                     -- RSS guid, unique per podcast
    title TEXT NOT NULL,
    published_at TEXT,                      -- ISO 8601
    audio_url TEXT NOT NULL,
    duration_seconds INTEGER,
    description TEXT,
    local_path TEXT,                        -- path relative to media_dir, NULL if not downloaded
    file_size_bytes INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | downloaded | transcribed | summarized
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(podcast_id, guid)
);
```

Insert default config on DB init:
```sql
INSERT INTO config (key, value) VALUES ('media_dir', './data/media/');
```

## CLI Commands

### `podracer search <query>`

Search Podcast Index for podcasts matching the query.

```
$ podracer search "market huddle"

  ID       Title                          Author
  ──────   ────────────────────────────   ──────────────
  947832   The Market Huddle              Patrick Ceresna & Kevin Muir
  102384   Market Huddle Weekly           ...

Found 2 results. Use `podracer episodes <id>` to browse episodes.
```

### `podracer episodes <podcast_id> [--limit N]`

Fetch and display episodes from the podcast's RSS feed. Saves podcast + episodes to DB.

```
$ podracer episodes 947832 --limit 5

  The Market Huddle — Patrick Ceresna & Kevin Muir
  ─────────────────────────────────────────────────

  #   Published    Duration   Title                              Status
  ──  ──────────   ────────   ─────────────────────────────────  ────────
  1   2026-04-07   1h 42m     Ep 285: Guest Name Here            pending
  2   2026-03-31   1h 38m     Ep 284: Craig Shapiro               downloaded
  3   2026-03-24   1h 51m     Ep 283: ...                        pending
  ...

Use `podracer download <episode_id>` to download an episode.
```

### `podracer download <episode_id>`

Download the audio file for an episode. Shows progress bar.

```
$ podracer download 42

Downloading: Ep 284: Craig Shapiro
[████████████████████████████████████████] 187.3 MB / 187.3 MB  100%

Saved to: ./data/media/the-market-huddle/ep-284-craig-shapiro.mp3
```

File naming: `<slugified-podcast-title>/<slugified-episode-title>.<ext>`

### `podracer subscribe <podcast_id>`

Mark a podcast as subscribed. Saves to DB.

### `podracer sync [--limit N]`

For each subscribed podcast, fetch latest episodes from RSS and download any new ones. `--limit` controls how many recent episodes to download per feed (default: 1).

## Dependencies

| Package | Purpose |
|---------|---------|
| `requests` | HTTP client (already a dependency) |
| `feedparser` | RSS/Atom feed parsing |
| `rich` | Terminal output formatting + progress bars |

## File Organization

```
podracer/
  search.py      # Podcast Index API client (search, auth headers)
  feed.py        # RSS feed parsing via feedparser
  download.py    # Episode download with progress
  db.py          # SQLite connection, migrations, queries
  config.py      # Config resolution (DB config table + env vars)
  cli.py         # Unified CLI entrypoint
```

## Implementation Notes

- **Podcast Index auth**: requires `X-Auth-Date` (epoch), `X-Auth-Key`, and `Authorization` (SHA-1 hash of key + secret + epoch). Straightforward to implement.
- **Idempotent downloads**: skip if `local_path` already exists and file size matches `file_size_bytes`
- **Graceful RSS parsing**: not all feeds have all fields — handle missing duration, description, etc.
- **No eval needed**: correctness is verifiable by inspection (does it download the right file? does the DB have the right data?)

## Acceptance Criteria

- [ ] `podracer search` returns results from Podcast Index
- [ ] `podracer episodes` lists episodes with correct metadata
- [ ] `podracer download` saves audio to the configured media_dir
- [ ] `podracer subscribe` + `podracer sync` downloads new episodes for subscribed shows
- [ ] SQLite schema is created on first run (automatic migrations)
- [ ] Duplicate downloads are skipped
- [ ] Works without GPU (pure Python + HTTP)
