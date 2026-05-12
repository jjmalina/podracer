# Web UI Plan

## Context

Podracer has a working CLI for processing podcasts (search, subscribe, download, transcribe, summarize). The user wants a web UI to browse processed episodes and search for new podcasts. The UI should be responsive (desktop, laptop, mobile) and use FastAPI + HTMX + a minimal CSS framework. Processing from the UI is deferred to later.

## Stack

- **FastAPI** — ASGI server
- **Jinja2** — server-side templates
- **HTMX** — dynamic updates without a JS framework (CDN)
- **Pico CSS** — classless/minimal CSS framework, responsive by default, styles semantic HTML out of the box (~10KB gzipped, CDN)
- **uvicorn** — ASGI runner

## New Dependencies

Add to `pyproject.toml`:
```
fastapi>=0.115
uvicorn[standard]>=0.30
jinja2>=3.1
```

## File Structure

```
podracer/web/
  __init__.py
  app.py                  # create_app() factory, lifespan, static/template mounts
  routes/
    __init__.py
    podcasts.py           # /podcasts, /podcasts/{id}
    episodes.py           # /episodes/{id}
    search.py             # /search, /search/results, /search/browse
  templates/
    base.html             # Layout: nav, Pico CSS + HTMX CDN links
    index.html            # Home/dashboard
    podcasts/
      list.html           # Table of all podcasts
      detail.html         # Podcast metadata + episode table
    episodes/
      detail.html         # Full episode view (speakers, summary, chapters, insights, takes)
    search/
      form.html           # Search input + RSS URL browse input
      _results.html       # HTMX partial: search results
      browse.html         # Episodes from an RSS feed
  static/
    style.css             # Minor overrides on top of Pico
```

## Routes

### Podcasts (`/podcasts`)

| Method | Path | Description | Template |
|--------|------|-------------|----------|
| GET | `/` | Redirect to `/podcasts` | — |
| GET | `/podcasts` | List all podcasts in DB | `podcasts/list.html` |
| GET | `/podcasts/{id}` | Podcast detail + episode table | `podcasts/detail.html` |

### Episodes (`/episodes`)

| Method | Path | Description | Template |
|--------|------|-------------|----------|
| GET | `/episodes/{id}` | Full episode detail: speakers, summary, chapters, insights, speaker takes | `episodes/detail.html` |

### Search (`/search`)

| Method | Path | Description | Template |
|--------|------|-------------|----------|
| GET | `/search` | Search form page | `search/form.html` |
| GET | `/search/results` | HTMX partial: search Podcast Index API | `search/_results.html` |
| GET | `/search/browse` | Browse episodes from `?feed_url=...` | `search/browse.html` |

## Template Details

**`base.html`**: HTML5 doc loading Pico CSS and HTMX from CDN. `<nav>` with links to Podcasts, Search. `{% block content %}` for page body.

**`podcasts/list.html`**: Table with columns: Title, Author, Episodes (count), Last Synced, Subscribed badge.

**`podcasts/detail.html`**: Podcast metadata (title, author, description, artwork). Episode table: Title, Published, Duration, Status (badge). Rows link to `/episodes/{id}`.

**`episodes/detail.html`**: The richest page. Sections:
- Header: title, podcast name (linked), published date, duration
- **Speakers**: table of name + role
- **Summary**: rendered as paragraphs
- **Chapters**: ordered list with timestamp, title, summary
- **Insights**: list with timestamp, speaker, text
- **Speaker Takes**: grouped by speaker, each with timestamp and take
- Fallback message if no summary exists

**`search/form.html`**: Search input with `hx-get="/search/results"` targeting `#results`. Separate RSS URL input with "Browse" button.

**`search/_results.html`**: HTMX partial with podcast cards (title, author, feed URL). "Browse Episodes" link on each.

**`search/browse.html`**: Feed metadata + episode table from RSS (FeedEpisode objects, not DB records).

## DB Changes

Add to `podracer/db.py`:

```python
def get_all_podcasts(conn) -> list[Podcast]:
    rows = conn.execute("SELECT * FROM podcasts ORDER BY title").fetchall()
    return [_podcast_from_row(r) for r in rows]

def get_episode_count(conn, podcast_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) as cnt FROM episodes WHERE podcast_id = ?", (podcast_id,)).fetchone()
    return row["cnt"]
```

## CLI Integration

Add `podracer serve` command to `podracer/cli.py`:

```python
def cmd_serve(args):
    from podracer.web.app import create_app
    import uvicorn
    cfg = _config()
    app = create_app(cfg)
    uvicorn.run(app, host=args.host, port=args.port)
```

Argparse:
```python
p_serve = subparsers.add_parser("serve", help="Start the web UI")
p_serve.add_argument("--host", default="127.0.0.1")
p_serve.add_argument("--port", type=int, default=8080)
p_serve.set_defaults(func=cmd_serve)
```

## Design Decisions

- **HTMX is targeted, not pervasive.** Full page loads for navigation. HTMX partials only for search results (avoid reload on each query).
- **Sync route handlers.** SQLite reads are sub-ms. FastAPI runs sync handlers in a threadpool. No async DB complexity.
- **No authentication.** Local-first tool on 127.0.0.1.
- **Template-rendered, not SPA.** Every route returns HTML. No JSON API for the UI.
- **Summary parsing.** Episode detail route parses `SummaryRecord.data` with `PodcastSummary.model_validate_json()`. Fallback message on parse failure.

## Implementation Sequence

1. Add dependencies to `pyproject.toml`
2. Add DB helpers (`get_all_podcasts`, `get_episode_count`) to `db.py`
3. Create `podracer/web/` package with `app.py` (create_app factory, lifespan for DB)
4. Create `base.html` with Pico CSS + HTMX
5. Implement podcasts routes + templates (list, detail)
6. Implement episode detail route + template
7. Implement search routes + templates
8. Wire `cmd_serve` into `cli.py`
9. Add `style.css` for minor overrides (status badges, timestamps)

## Verification

1. Run `podracer serve` and open `http://localhost:8080`
2. Verify podcast list shows all podcasts in DB
3. Click into a podcast, verify episode table with correct statuses
4. Click into a summarized episode, verify all sections render (speakers, summary, chapters, insights, takes)
5. Test search: query Podcast Index, browse episodes from RSS URL
6. Test responsive: resize browser to mobile width, verify layout adapts
7. Test on phone by binding to `--host 0.0.0.0`

## Files to Modify

- `pyproject.toml` — add dependencies
- `podracer/db.py` — add `get_all_podcasts()`, `get_episode_count()`
- `podracer/cli.py` — add `cmd_serve` and argparse entry

## Files to Create

- `podracer/web/__init__.py`
- `podracer/web/app.py`
- `podracer/web/routes/__init__.py`
- `podracer/web/routes/podcasts.py`
- `podracer/web/routes/episodes.py`
- `podracer/web/routes/search.py`
- `podracer/web/templates/base.html`
- `podracer/web/templates/index.html`
- `podracer/web/templates/podcasts/list.html`
- `podracer/web/templates/podcasts/detail.html`
- `podracer/web/templates/episodes/detail.html`
- `podracer/web/templates/search/form.html`
- `podracer/web/templates/search/_results.html`
- `podracer/web/templates/search/browse.html`
- `podracer/web/static/style.css`

## Existing Code to Reuse

- `podracer/db.py` — all existing query functions
- `podracer/models.py` — Podcast, Episode, Transcript, SummaryRecord
- `podracer/summarize.py` — PodcastSummary, Chapter, Insight, SpeakerTake, SpeakerIdentification
- `podracer/search.py` — search_podcasts()
- `podracer/feed.py` — fetch_feed_metadata(), fetch_episodes()
- `podracer/config.py` — load_config()
