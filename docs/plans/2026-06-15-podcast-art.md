# Podcast artwork (cover art)

**Date:** 2026-06-15
**Status:** Planned

## Goal

Show each podcast's square "album art" cover across the UI so podracer reads as a
real podcast app, not a database table. Art appears in **search results**, on the
**podcast page**, in the **episode header**, and — as a later phase — in the
**podcast list / feed view**.

Cover images are **cached on disk and served by the app**, not hotlinked from
podcast CDNs. We already learned (commit `dc0679c`) that some hosts 403 requests
without a `User-Agent`; the server controls its UA, the browser doesn't. Caching
also keeps the local-first promise: once subscribed, a podcast renders with zero
external requests.

## What already exists

- **`podcasts.artwork_url`** is in the schema (`db/connection.py:19`) and is
  populated at subscribe time from the feed's `<image>` / `itunes:image`
  (`feed.py:82`, `fetch_feed_metadata`). No new ingestion is needed to *know* the
  URL — only to fetch and display it.
- **`download.py`** already has everything the fetch step needs: `slugify()`, the
  `USER_AGENT` constant, and an `httpx` download pattern. Audio for a podcast
  already lands in `{media_dir}/{podcast_slug}/`; the cover joins it there.
- **`_migrate()`** (`db/connection.py:99`) is an idempotent
  `PRAGMA table_info` → `ALTER TABLE ADD COLUMN` runner. Adding a column is a
  three-line change, exactly like the `subscribed_at` precedent.

The only thing missing is: fetch the bytes, remember where they are, and put an
`<img>` in the templates.

## Approach: cache locally, serve from the app

```
Worker sync (off the request path)
  podcast.artwork_url ──HTTP (our User-Agent)──▶ {media_dir}/{slug}/cover.jpg
                                                  └─ path stored in podcasts.artwork_path

Browser
  <img src="/podcasts/{id}/artwork">  ──▶  FileResponse from local disk
                                            (falls back to a bundled SVG if absent)
```

Subscribed podcasts are served from our own route, so templates can be dumb and
never render a broken image. Pre-subscription previews (live search, feed browse)
are the one exception — see "Previews" below.

## Pieces in detail

### 1. Schema + model

`db/connection.py` — add to the `podcasts` CREATE TABLE (for fresh DBs) and to
`_migrate()` (for existing ones):

```python
# in _migrate(), alongside the existing podcast-column checks
if "artwork_path" not in pc_cols:
    conn.execute("ALTER TABLE podcasts ADD COLUMN artwork_path TEXT")
```

`models.py` — add `artwork_path: str | None = None` to `Podcast`. Check that the
row→model mapping in `db/podcasts.py` (get/list queries) carries the new column
through.

### 2. Fetch helper

`download.py` — sibling to `download_episode`, reusing `slugify` + `USER_AGENT`:

```python
ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def download_artwork(artwork_url: str, media_dir: str, podcast_title: str) -> str:
    """Download a podcast cover and return its media-relative path."""
    ext = Path(urlparse(artwork_url).path).suffix.lower()
    if ext not in ARTWORK_EXTS:
        ext = ".jpg"
    relative_path = f"{slugify(podcast_title)}/cover{ext}"
    full_path = Path(media_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(artwork_url, follow_redirects=True, timeout=30.0,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    full_path.write_bytes(resp.content)
    return relative_path
```

### 3. DB write + worker integration

`db/podcasts.py` — add `set_podcast_artwork_path(conn, podcast_id, path)`.

A small idempotent orchestrator (in `download.py` or a new `artwork.py`):

```python
def ensure_artwork_cached(conn, podcast, media_dir) -> None:
    if not podcast.artwork_url:
        return
    if podcast.artwork_path and (Path(media_dir) / podcast.artwork_path).exists():
        return  # already cached
    path = download_artwork(podcast.artwork_url, media_dir, podcast.title)
    set_podcast_artwork_path(conn, podcast.id, path)
```

**Primary trigger — subscribe.** Call `ensure_artwork_cached` in the subscribe flow
(`search/subscribe`) right after `upsert_podcast`, so the cover is copied the moment
you subscribe and the podcast page renders it immediately — no waiting for a sync
tick. **Backstop — worker sync.** Also call it from `worker.py::_sync_feeds` for
each podcast (no-op once cached), wrapped in the loop's existing try/except +
`sentry_sdk.capture_exception()`, to heal anything the subscribe-time copy missed
(host was down, etc.). A flaky image host never breaks a subscribe or a sync.

> Refetch policy (v1): cache when `artwork_path` is null or the file is gone. Two
> refinements left for later: refetch when `artwork_url` *changes*, and re-fetch feed
> metadata when a cover download 4xxs (a *non-null but dead* URL — see the Market
> Huddle case below — which the null-URL branch doesn't catch). Neither is worth the
> complexity for v1; a dead URL simply shows the fallback SVG.

### 4. Serving route + generated placeholder

The route returns the cached file when present, else a **generated, per-podcast
colored placeholder** — so a list of missing covers reads as distinct tiles, not a
column of identical gray squares. The placeholder is a deterministic color + the
podcast's initial.

`web/routes/podcasts.py`:

```python
# Muted, instrument-panel tints — visible on near-black, no garish green.
PLACEHOLDER_TINTS = [
    "#8a5a2b", "#9c6b2e", "#7d6a2f", "#566b3c", "#3c6b62",
    "#3c5a7d", "#5b4a7d", "#7d3c5a", "#8a4433", "#6b5440",
]

def _placeholder_svg(seed: int, letter: str) -> str:
    tint = PLACEHOLDER_TINTS[seed % len(PLACEHOLDER_TINTS)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" fill="{tint}"/>'
        '<text x="50" y="50" text-anchor="middle" dominant-baseline="central" '
        'font-family="Space Grotesk, sans-serif" font-size="46" font-weight="600" '
        f'fill="#e7e5dd" opacity="0.9">{escape(letter)}</text>'
        '</svg>'
    )

@router.get("/podcasts/{podcast_id}/artwork")
def podcast_artwork(podcast_id: int, request: Request, conn=Depends(get_db)):
    podcast = get_podcast(conn, podcast_id)
    media_dir = request.app.state.cfg.media_dir
    if podcast and podcast.artwork_path:
        path = Path(media_dir) / podcast.artwork_path
        if path.exists():
            return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    letter = (podcast.title.strip()[:1].upper() if podcast and podcast.title else "") or "?"
    seed = podcast.id if podcast else 0
    return Response(_placeholder_svg(seed, letter), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})
```

Why this shape:

- **Deterministic, and distinct-in-a-list.** `id % len(palette)` makes a podcast's
  tile color stable, and because ids are sequential the first N podcasts each land on
  a different tint — exactly the "different colors in a list" goal. The initial is a
  second distinguishing signal (color *and* letter vary).
- **On-brand.** Muted earth/slate tints tuned to sit on the near-black UI, light
  monogram in the theme ink (`--pr-text`). Tweak `PLACEHOLDER_TINTS` to taste; greens
  are kept to muted olive per the design language (saturated green was rejected for
  the accent).
- **Templates stay dumb.** Every subscribed surface is just
  `<img src="/podcasts/{id}/artwork">`; the route always returns valid SVG or an
  image, so a subscribed podcast never shows a broken-image icon. `escape()` the
  letter — titles can contain markup-significant characters.
- **Cache nuance.** Real covers get `max-age=86400`; the placeholder branch sets
  `no-cache` so the browser re-asks and picks up the real cover the instant it's
  cached (e.g. after the worker heals a briefly-down host) instead of pinning the
  placeholder for a day.

A dedicated route (rather than mounting `media/` as static) keeps audio files
unexposed and centralizes this fallback logic. Needs `Response` + `escape`
(`markupsafe`) imported into the route module.

**Pre-subscription previews** (hotlinked search/browse — no id yet, so they can't use
this route): give those `<img>`s an `onerror` that swaps to a neutral static
`static/cover-fallback.svg`, so a dead preview URL degrades to a placeholder rather
than a broken-image icon. (Optional: a `/placeholder?seed=&letter=` route to give
previews colored tiles too — defer unless wanted.)

### 5. Templates + CSS

Square art, flat, `2px` radius and a `1px solid var(--pr-line)` border to match the
instrument-panel surfaces. `aspect-ratio: 1/1; object-fit: cover;` so non-square
sources don't distort.

| Surface | Template | Treatment |
|---|---|---|
| Podcast detail | `podcasts/detail.html` | Cover (~140px) in the header beside title/author/description. |
| Episode header | `episodes/detail.html` | Parent podcast's cover (~64px) next to the episode title (route already passes `podcast`). |
| Search results | `search/_results.html` | Thumbnail (~56px) in each result card. *(preview — see below)* |
| Feed browse | `search/browse.html` | Cover in the feed-preview header. *(preview — see below)* |
| Podcast list | `podcasts/list.html` | **Phase 2** — small thumbnail cell, or a redesign into a grid of cover tiles. |

New CSS tokens/classes in `style.css` (e.g. `.cover`, `.cover-sm`, `.cover-hero`).

### Previews (pre-subscription surfaces)

Search results come from the Podcast Index API and the browse page from
`fetch_feed_metadata` — neither podcast is in our DB yet, so there's no `id` to
serve and nothing cached. For these **previews only**, hotlink the upstream image
URL directly (Podcast Index serves browser-friendly CDN thumbnails; the feed's
`artwork_url` for browse). Verify the exact image field on the search-result object
when wiring it up. Once the user subscribes, `ensure_artwork_cached` takes over and
every later render is local.

> If preview hotlinks turn out flaky (a feed's raw CDN 403s the browser on the
> browse page), the hardening is a tiny server-side image proxy
> (`GET /artwork-proxy?url=...`, fetched with our UA, http(s)-only). Deferred — not
> needed unless it actually breaks.

## Image sizing

Measured 2026-06-15 (the 5 subscribed covers): **0.4–1.4 MB**, JPEG/PNG, full-res
~3000² masters (the Apple Podcasts standard). They render at **≤200px** (hero ~140,
thumbs ~56–64).

**Decision: serve the original copy; no thumbnail pipeline in v1.** One cover per
page, browser-cached via `Cache-Control`, over LAN — 1.4 MB is a non-issue, and
Phase 1 stays dependency-free (no Pillow).

The *one* surface where originals bite is the future **grid/list view** (Phase 4)
that renders many covers at once — and the cost there is **browser decode memory**,
not bandwidth: a 3000² image decodes to ~36 MB regardless of CSS display size, so
~30 tiles ≈ ~1 GB of decode (`width:` doesn't reduce it). So tie the resize decision
to Phase 4, and when it lands prefer a **single downscale-on-copy** — one
`Image.thumbnail((640, 640))` call in `download_artwork` (only shrinks, never
upscales), normalized to JPEG — over a multi-size or on-the-fly pipeline. One ~40–80
KB capped copy then serves every surface; originals stay re-fetchable from
`artwork_url`, and re-processing existing covers is just a `backfill-artwork` re-run.

## Migrating an existing DB

The schema change and the image backfill are two separate, time-decoupled steps.

**Schema — automatic, no manual step.** `init_db()` runs `_migrate()` on every
startup (web lifespan `app.py:65`, and every CLI/worker entry), so shipping the new
`_migrate` line + restarting is the whole migration. On SQLite, `ADD COLUMN` is
metadata-only (instant, no row rewrite); the column lands `NULL` on all existing
rows. Idempotent and non-destructive — the same path `subscribed_at` took.

**Backfill — fetch the bytes for the now-`NULL` column.** Two composable paths,
both built on the idempotent `ensure_artwork_cached` (no-op once the file exists):

- *Passive:* the `_sync_feeds` hook caches every podcast with an `artwork_url` on the
  next tick (≤ sync interval, or on a manual sync). Self-healing; do nothing.
- *Active:* a `podracer backfill-artwork` CLI subcommand (fits the
  CLI-as-agent-interface principle) for immediate, logged population:

  ```python
  def cmd_backfill_artwork(args):
      cfg = _config()
      conn = get_connection(cfg.db_path)
      init_db(conn)                       # guarantees the column exists first
      for p in get_subscribed_podcasts(conn):
          try:
              if not p.artwork_url:        # feeds that never exposed an image
                  meta = fetch_feed_metadata(p.feed_url)
                  if meta.artwork_url:
                      set_podcast_artwork_url(conn, p.id, meta.artwork_url)
                      p = get_podcast(conn, p.id)
              ensure_artwork_cached(conn, p, cfg.media_dir)
              conn.commit()
          except Exception:
              logger.exception("artwork_backfill_failed", podcast=p.title)
              sentry_sdk.capture_exception()
  ```

Current DB state (2026-06-15): 7 podcasts, 5 subscribed, **all 5 already have an
`artwork_url`** — so backfill is purely "fetch 5 images"; the `if not p.artwork_url`
re-fetch branch is future-proofing, exercised by 0 rows today. **Caveat:** 1 of the
5 (The Market Huddle) returns **404** for its stored `artwork_url`, so it shows the
fallback placeholder until the URL is refreshed. That's a *non-null dead* URL, which
the null-URL branch above won't heal — a good live test of the placeholder path, and
the motivation for the deferred "re-fetch metadata on 4xx" refinement.

The UI is correct in every intermediate state: between "column added" and "bytes
cached," the serving route returns the fallback SVG, so there's no flag-day
ordering. Rollout: ship → restart (column appears, all `NULL`) → `backfill-artwork`
(or wait one sync tick) → covers render.

## Phasing

1. **Plumbing** — schema column, model field, `download_artwork`,
   `set_podcast_artwork_path`, `ensure_artwork_cached`, worker hook, the
   placeholder generator + a neutral static `cover-fallback.svg` for previews.
2. **Primary surfaces** (the requested win) — serving route + podcast detail,
   search results, feed browse, episode header. Add `ensure_artwork_cached` to the
   subscribe path for instant covers.
3. **Backfill** — ship `podracer backfill-artwork` and run it once (see
   "Migrating an existing DB"); existing subscriptions also heal on the next sync.
4. **Feed/list view (future todo)** — bring covers to `/podcasts`, likely
   redesigned from the current table into a grid of cover tiles.

## Out of scope

- **Per-episode artwork.** Many feeds carry `itunes:image` per `<item>`; episodes
  have no `artwork_url` column today. Possible later: extend `fetch_episodes` +
  episodes schema and fall back to the podcast cover when absent.
- **Image resizing / thumbnails.** v1 serves originals; a single downscale-on-copy
  is deferred to the grid phase — see *Image sizing*. Multi-size variants and
  on-the-fly resizing stay out of scope.
- **Re-fetch on URL change.** v1 caches once; staleness is acceptable for cover art.
```
