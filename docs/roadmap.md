# Roadmap

Future features — captured as short sketches. Each entry is problem +
rough approach, not a full plan. Promote an entry into
`docs/plans/<date>-<slug>.md` when you're ready to build it.

---

## Site logo

**Problem.** The web UI's nav bar is plain text ("podracer"). A logo
gives the site identity and makes it instantly recognizable in browser
tabs.

**Sketch.**
- SVG asset committed to `podracer/web/static/logo.svg` so it scales
  cleanly at any size. Optionally also a 32×32 favicon.
- Drop the existing `<strong><a href="/podcasts">podracer</a></strong>`
  in `base.html` for an `<img src="/static/logo.svg" alt="podracer"
  height="28">` (kept inside the same link so it routes home on click).
- Add `<link rel="icon" type="image/svg+xml" href="/static/logo.svg">`
  in `<head>` for the browser-tab favicon.
- Theme variant: if the logo is dark-on-light, supply a light-on-dark
  version too; use CSS `@media (prefers-color-scheme: dark)` to swap, or
  honor the existing `data-theme` toggle already in `base.html`.
- Asset itself: hand-drawn, generated (Midjourney / Sora), or
  commissioned — out of scope for the code change.

---

## Podcast artwork in the UI

**Problem.** All subscribed podcasts look identical in the list view —
just titles in a table. Adding the show's artwork makes the archive
feel like a podcast app rather than a database admin panel and lets you
visually pick a show at a glance.

**Sketch.**
- The data is already there: `podcasts.artwork_url` gets populated by
  `fetch_feed_metadata` during subscribe/sync. Most feeds expose 1400×1400
  iTunes artwork.
- Render the URL directly in the podcasts list + detail pages as
  `<img src="{{ podcast.artwork_url }}" loading="lazy">` at maybe
  72×72 in the list and 240×240 on the detail page. No download needed.
- Possible upgrade: cache artwork locally under
  `<media_dir>/artwork/<podcast_id>.{jpg,png}` and serve from
  `/static/artwork/...`. Reasons:
  - feeds occasionally rotate/expire the original URL
  - LAN-only deployments don't have to hit the publisher's CDN per
    pageview
  - thumbnails can be resized to a single size on download instead of
    pulling the full 1400×1400 every time
- DB doesn't need to change for the cached version — add a
  `<media_dir>/artwork/` convention and a sync-time download in
  `_sync_feeds()` / `upsert_podcast`.
- Episode-level artwork (some feeds set per-episode) is a follow-up:
  add `episodes.artwork_url`, render it on the episode detail page as a
  fallback when the podcast-level image isn't distinctive enough.

---

## Per-podcast custom summarization instructions

**Problem.** The summarization prompts are one-size-fits-all: every
podcast gets summary/chapters/insights/speaker_takes regardless of
genre. A financial podcast (Market Huddle, Odd Lots) really wants
"trade ideas" or "macro calls" extracted as a separate, structured
section. An academic podcast wants "studies cited". A founder interview
wants "advice for early-stage founders". Hard-coding more sections in
`summarize.py` doesn't scale.

**Sketch.** Two-phase: a simple v1 that's useful, a v2 that's properly
structured.

**v1 — free-form extra section (small change):**
- Schema: add `podcasts.summarize_instructions TEXT` via the same
  migration pattern in `db/connection.py::_migrate`. NULL means "use
  defaults".
- Prompt: append the instructions to a new prompt that returns one
  extra field on `PodcastSummary`:
  ```python
  class PodcastSummary(BaseModel):
      ...
      custom_notes: str | None = None   # markdown, free-form
  ```
  Run a 6th LLM pass with `CUSTOM_PROMPT = "Given a podcast transcript
  and the following user instructions: {instructions}. Return the
  requested information as markdown."` Only runs when the podcast has
  non-NULL instructions.
- UI: add a `<textarea>` to the podcast detail page; POST to
  `/podcasts/{id}/instructions`. Render `custom_notes` on the episode
  page as a "Notes" section if present.
- Tests: round-trip on the podcasts table; the LLM pass is mocked.

**v2 — typed custom fields (bigger lift):**
- Per-podcast schema: instructions become a small list of named fields
  with types, e.g. `{"trade_ideas": "list of {ticker, thesis,
  timestamp}", "macro_calls": "list of {claim, supporting_quote}"}`.
- Build a Pydantic model dynamically (via `create_model`) and pass its
  JSON schema as `response_format` to the LLM — same path the existing
  five prompts use.
- DB shape: probably a separate `podcast_summary_fields` table with
  `(podcast_id, name, type_spec)` rows, or a JSON column on `podcasts`.
- UI gets richer: per-field editor + per-episode rendering that knows
  the field types (lists render as tables, etc.).

**Open questions.**
- Should custom instructions be one-shot per podcast or per-episode
  overridable? Probably per-podcast for v1 (config), per-episode flag
  for one-off needs in v2.
- Idempotency: if instructions change, do existing summaries get
  re-run? Probably no by default — add a "re-summarize" button on the
  podcast page that bulk-enqueues summarize jobs with `--force`.
- Cost: an extra LLM pass per episode roughly +20% summarize cost.
  Cheap on DeepSeek V4 Flash.

---

## Chat widget: "ask this episode / podcast"

**Problem.** The summary tells you what was said. But the long tail of
listening is the followups: "what did Joe mean by that?", "what
podcasts did Megan reference?", "summarize her thoughts on UK
inflation in two sentences I can paste in a Slack". Today that
requires Ctrl-F across the transcript. An LLM chat widget grounded in
the episode (or podcast) is the natural next step on top of
transcripts + summaries.

**Sketch.** Three tiers of scope, build in this order:

**v1 — per-episode chat.** Whole transcript fits in context for most
podcasts (<4 hr ≈ <60k tokens; DeepSeek V4 Flash has 1M).
- Backend: new route `POST /episodes/{id}/chat` taking a user message
  + (optional) conversation history. Server-side stuffing prompt:
  `{podcast description} + {show notes} + {summary} + {transcript} +
  {history} + {user message}`. Reuses the existing `summarize.Backend`
  dispatch so it works with OpenRouter / Ollama / vLLM out of the box.
- Streaming via SSE: hook `/episodes/{id}/chat/stream` returning
  `text/event-stream`. The client appends tokens as they arrive.
- Persistence: new tables `conversations(id, episode_id, created_at)`
  and `messages(id, conversation_id, role, content, created_at)`.
  Replay across sessions so you can pick up where you left off.
- UI: sticky panel at the bottom of the episode page (or right
  sidebar on wider viewports). Pico CSS already in `base.html`, no
  framework needed; htmx is already loaded for free streaming.
- Anchored citations are the killer feature: when the LLM mentions a
  specific point, link it back to the chapter / timestamp in the
  transcript. Prompt the model to include `[HH:MM:SS]` references and
  let the client render those as `<a href="#t-HHMMSS">` jumps.
  (Pairs well with the audio-player-sync roadmap item.)

**v2 — per-podcast chat.** Too much context to stuff naively — a
single subscribed podcast can have hundreds of episodes.
- Requires the embeddings + vector-search roadmap item to be in place.
  Retrieval over chunked transcripts (per-episode summary or per-
  chapter for finer grain), top-k passed into the chat prompt with
  episode/timestamp metadata.
- UI: same widget, but reachable from `/podcasts/{id}` and filtered to
  episodes of that podcast.

**v3 — archive-wide chat.** Same retrieval stack, no podcast filter.
"What has Patrick Ceresna said about copper this year?" across every
subscription. Natural fit for the planned MCP server entry — agents
get the same tool internally.

**Open questions.**
- Cost control: cap context-stuffed chat to N tokens (truncate
  transcript) to keep per-message cost predictable.
- Auth: LAN-only is fine for now. If the site ever goes public-internet,
  put a single shared password in front of the chat route at minimum
  — otherwise it's an open LLM proxy.
- Model choice: separate config knob so chat can use a more capable
  model than summarization (different cost/quality tradeoff).

---

## Agentic search across podcasts and episodes (FTS + vectors)

**Problem.** Right now the only way to find something in the archive
is browse-by-title. Even the existing summaries aren't searchable.
You can't ask "who has talked about copper this quarter?" or "every
episode that cited a Stanford study". The chat widget's v2/v3 modes
depend on this same retrieval stack — solve it once, both features
land.

**Sketch.** Two retrieval channels (FTS for exact, vectors for
semantic) wrapped in a small tool-use loop the LLM can call.

**Full-text search (start here — simplest, no GPU).**
- SQLite FTS5 virtual table mirroring `transcripts(text, episode_id)`
  plus a separate FTS index over summary fields (chapters titles,
  insights text, speaker takes). FTS5 ships with SQLite, no new
  dependency.
- Keep FTS in sync via triggers on insert/update of transcripts +
  summaries.
- Surface as `GET /search?q=...&kind=fts&podcast_id=...` returning
  episode + snippet matches with `bm25()` ranking.

**Vector search.**
- Chunk transcripts at chapter boundaries (1–5 min slices) for
  embedding. Each chunk gets `(episode_id, chapter_idx, start_ts,
  end_ts, text)`.
- Embedding storage: start with `sqlite-vec` extension — keeps
  everything in one DB file, no extra service to deploy. Move to
  Qdrant later if scale demands it (probably not, at personal scale).
- Embedding model: BGE or nomic-embed locally on the GPU host (sits
  alongside whisper-service in the v2 deployment), or
  OpenAI/Voyage embeddings for the cloud-only path. Pluggable like
  the transcribe/summarize backends.
- Job pipeline: add an `embed` job kind that runs after `summarize`
  succeeds, indexes the new transcript's chunks. Reuses the existing
  queue + retry + watermark machinery — no new daemon needed.

**Agentic search loop.**
- Expose retrieval as LLM tools, not direct endpoints. Tools:
  `search_fts(q, podcast_id?, limit)`,
  `search_vector(q, podcast_id?, limit)`,
  `get_episode(id)` (returns summary + key timestamps),
  `get_chapter(episode_id, idx)` (returns the chunk text).
- The agent decides: rephrase the query, run FTS for proper nouns and
  vectors for concepts, filter by podcast, re-rank, fetch the actual
  passages, then answer with citations linking back to
  `/episodes/{id}#t-HHMMSS`.
- Implementation: same chat backend as the chat-widget item, just
  with tools registered. Anthropic / OpenRouter / Ollama all support
  tool use over the same OpenAI-style schema, so the dispatcher in
  `summarize.py` extends naturally.

**UI surfaces.**
- Promote the existing `/search` page from "podcast lookup" to a
  unified search box that hits this stack. Two tabs: "Find episodes"
  (current podcast-search behavior) vs "Ask the archive" (agentic).
- Result rows show episode, podcast, snippet, and the chapter
  timestamp. Click → episode page scrolled to that chapter.

**Open questions.**
- Re-index strategy when an episode is re-transcribed (e.g.
  whisperx-http after Deepgram): drop old chunks first, re-embed
  fresh. `episode_id` is the natural delete key.
- Embedding model migration: if you switch from BGE to nomic later,
  the whole corpus needs re-embedding. Store the embedding model name
  alongside the vector so we can detect mixed states.
- Recall vs cost: vector search over 10k+ chunks is fast in
  sqlite-vec; embedding the corpus once is the expensive part
  (~$0.10 cloud or ~30 min local for the current archive).

---

## One-off YouTube video transcript + summary

**Problem.** Lots of long-form content I want transcribed is on
YouTube, not in an RSS feed: lectures, interviews, conference talks,
the random 90-min video someone DMs you. Today there's no way to
ingest those — the whole pipeline is bolted to RSS feeds and audio
file URLs.

**Sketch.** Treat a YouTube URL as a one-off "episode" not attached
to any subscribed podcast. Reuse the existing transcribe + summarize
stages with minimal new code.

- New CLI: `podracer ingest <url>`. Accepts a YouTube URL (or
  anything `yt-dlp` understands — Twitter videos, Vimeo, direct MP4
  links). Workflow:
  1. `yt-dlp -x --audio-format mp3 --no-playlist <url>` → drops an mp3
     under `<media_dir>/oneoff/<video_id>.mp3` plus the JSON metadata.
  2. Create or reuse a synthetic "Ad-hoc" podcast row (single row,
     `subscribed = 0`, used as a catch-all parent for one-offs).
  3. Insert an episode with `audio_url = <youtube URL>`,
     `local_path = oneoff/<video_id>.mp3`,
     `title = <yt-dlp title>`,
     `description = <yt-dlp description>` (this is the YouTube show
     notes, often pretty rich),
     `published_at = <yt-dlp upload_date>`.
  4. Enqueue transcribe + summarize via the existing pipeline. Worker
     processes it like any episode.
- Web UI: `POST /ingest` form on a new `/ingest` page (or the
  existing search page) that takes a URL, calls the same backend
  helper, and redirects to the resulting episode's detail page.
- Dependency: `yt-dlp` as a system package (Debian:
  `apt install yt-dlp`) or pip extra. Lean toward the apt route — it
  needs to stay updated against YouTube's frontend churn and apt
  packages get refreshed more reliably than a pinned pip dep.
- Storage: the "Ad-hoc" podcast row keeps one-offs from polluting the
  subscriptions list while making them browsable in the same UI. They
  still get a chat widget, search hits, etc. for free.

**Open questions.**
- YouTube channel feeds (RSS exists for channels —
  `https://www.youtube.com/feeds/videos.xml?channel_id=…`) could
  be subscribed to like a podcast, but the audio URL is the YouTube
  video itself. That's a separate "YouTube channel subscription"
  feature; this one stays scoped to single-URL ingest.
- Cookies / age-gated content: yt-dlp supports `--cookies-from-browser`
  for stuff that needs a login. Punt unless it matters.
- Speaker diarization on a single-presenter lecture is wasted work but
  cheap; default to leaving it on, add a `--no-diarize` flag to
  `ingest` for the rare case.

---

## Job management: parallelism + re-run stages

**Problem.** The worker drains the queue one job at a time. Made sense
when transcription held a single GPU; doesn't make sense now that the
default path is Deepgram + OpenRouter HTTP calls. A backlog of 10
episodes that could finish in 5 minutes if pipelined takes ~30+
minutes today. Separately, there's no good UI to re-run a stage
after the fact: changed your custom summarization instructions?
Re-transcribed with a better model? You have to either delete the
existing row by hand or `podracer summarize <id> --force` from the
CLI.

**Sketch.**

**Parallelism in the worker:**
- Move the drain loop to asyncio with N concurrent `claim_next_job`
  workers (a small `asyncio.Semaphore` controls fan-out). Each
  coroutine claims, runs (via `await asyncio.to_thread(...)` for the
  blocking httpx + Deepgram SDK calls), commits, repeats.
- Two concurrency knobs in `[daemon]` config:
  `transcribe_concurrency = 3`, `summarize_concurrency = 5`. Defaults
  conservative so personal-scale users don't get rate-limited.
  Per-stage because the stages have different cost profiles + rate
  limits.
- `claim_next_job` already uses `UPDATE … WHERE id = (SELECT … LIMIT 1)
  RETURNING *`, which is atomic in SQLite — no extra locking needed.
- Backoff: when Deepgram/OpenRouter return 429, the existing tenacity
  retry handles it. Add a process-wide "rate-limit observed, briefly
  reduce concurrency" signal for the case where every concurrent
  worker hits 429 at once. Cheap implementation: shared
  `asyncio.Event` that pauses new claims for 30s.
- Tests: the existing in-memory SQLite fixture works; add tests for
  concurrent claims (no two coroutines claim the same job) and for
  the rate-limit pause behavior.

**Re-run from the UI:**
- New job kinds aren't needed — reuse `transcribe` + `summarize` with
  a `force` column.
- Schema: add `jobs.force INTEGER NOT NULL DEFAULT 0`. The worker's
  dispatch passes it through to `transcribe_episode(..., force=bool(j.force))`
  / `summarize_episode(..., force=...)`. (Note: the active-job uniq
  index doesn't conflict — a forced re-run only enqueues when there's
  no current active job for that episode + kind.)
- New helpers in `db/jobs.py`:
  `enqueue_transcribe(conn, episode_id, force=True)`,
  `enqueue_summarize(conn, episode_id, force=True)`. The existing
  `enqueue_episode_pipeline` stays for the "process both" path.
- UI: on the episode detail page, add three buttons when artifacts
  exist:
  - "Re-transcribe" — enqueues just transcribe, force=True
  - "Re-summarize" — enqueues just summarize, force=True (assumes
    transcript exists)
  - "Re-process" — re-runs both as a chained pipeline
- On the `/jobs` page, the Retry button for failed jobs is unchanged
  (resets attempts, clears error). Add a "Re-run" button on
  recently-done jobs that creates a fresh forced job.
- Bulk action: on the podcast detail page, a single "Re-summarize all
  episodes" button that enqueues a summarize-only forced job per
  episode that already has a transcript. Useful after changing
  custom instructions (see entry #4).

**Open questions.**
- Concurrent transcribe + concurrent summarize for the same episode:
  the dependency clause already prevents summarize from claiming
  before its parent transcribe is done, so this is naturally
  correct — just makes sure summarize concurrency draws from a
  separate semaphore.
- Worst-case rate limiting: at concurrency 5 on OpenRouter, a Macro
  Voices backfill could spike requests. Per-backend rate-limit budget
  config? Probably overkill for v1 — tenacity backoff handles 429s
  gracefully, and the spike-then-relax pattern is fine for a personal
  archive.
- The `/jobs` page meta-refresh (10s) is fine for view-only state
  display; bumping to 5s might feel snappier with parallelism, but at
  some point swap to htmx polling of just the counts block (small
  improvement, not worth doing now).

---

## REST API (JSON) for agent + script access

**Problem.** Today the only programmatic access to podracer is shelling
into the CLI. Agents (Claude, custom scripts, n8n / Zapier flows, a
voice assistant) can't pull a transcript or kick off processing
without scraping HTML. A clean JSON API makes podracer composable —
the same backend that serves the web UI also serves agents.

**Sketch.**

**Surface.**
- New router under `/api/v1/...`, mounted alongside the existing HTML
  routes. FastAPI renders both from the same app — the existing
  Pydantic models (`Podcast`, `Episode`, `Transcript`, `Summary`,
  `Job`) serialize directly as JSON with no extra work.
- Endpoints, mostly mirroring the CLI:
  - `GET  /api/v1/podcasts` — list subscriptions
  - `GET  /api/v1/podcasts/{id}` — one podcast
  - `POST /api/v1/podcasts` — `{feed_url}` → subscribe (calls the same
    helper as the CLI/UI; auto-queues latest unless `?queue=false`)
  - `DELETE /api/v1/podcasts/{id}` — unsubscribe
  - `GET  /api/v1/podcasts/{id}/episodes?limit=&offset=&status=`
  - `GET  /api/v1/episodes/{id}` — episode metadata + flags
    (has_transcript, has_summary, active_job)
  - `GET  /api/v1/episodes/{id}/transcript` — full text (huge)
  - `GET  /api/v1/episodes/{id}/summary` — the `PodcastSummary` JSON
  - `POST /api/v1/episodes/{id}/process` — enqueue (with optional
    `{force: true, stage: "transcribe"|"summarize"|"both"}`)
  - `GET  /api/v1/jobs?status=...&limit=...` — queue inspection
  - `POST /api/v1/jobs/{id}/retry`, `DELETE /api/v1/jobs/{id}` —
    actions matching the /jobs page
  - `POST /api/v1/ingest` — `{url}` for the one-off YouTube ingest
    (entry #8)
  - `POST /api/v1/episodes/{id}/chat` — when the chat widget lands,
    same backend exposed via API
- OpenAPI / Swagger UI come free at `/api/v1/docs`. Make sure the
  existing UI routes are excluded from the auto-generated schema so
  it stays focused.

**Auth.**
- Single shared bearer token in config, e.g. `[api] tokens = ["..."]`.
  Reads `Authorization: Bearer <token>`. LAN-trust until podracer
  becomes multi-user.
- Optional `[api] public_read = true` flag to allow unauthenticated
  GETs (read-only) but require auth for POST/DELETE — useful when the
  homelab deploy is behind a private VPN or Tailscale.
- The HTML routes stay open as today; auth only applies to `/api/...`.

**Pagination + filtering.**
- `?limit=`, `?offset=` everywhere it matters. Default `limit=50`,
  hard cap `limit=500`.
- `?since=<iso>` on episode + jobs listings so agents can poll
  incrementally instead of pulling the world.

**Compatibility.**
- Add `/api/v1/version` returning `{podracer_version, schema_version}`.
  Bumps when breaking changes happen — clients pin against a major.
- Don't return raw SQLite row dicts; route through Pydantic models so
  the response shape is the contract.

**MCP follow-up.**
- An MCP server (already mentioned in the README roadmap) is a thin
  wrapper over this API: each MCP tool maps to one REST endpoint.
  Build the REST API first, then expose 6–10 tool surfaces over it.

**Open questions.**
- Webhooks for "job done" so agents can react instead of polling?
  Tempting but yagni until something asks for it.
- Streaming responses for transcripts / chat: chat will need SSE
  anyway (see entry #5). Other endpoints can stay plain JSON.
- API versioning strategy when fields change: additive in v1
  (Pydantic ignores extras by default on the client); reserve v2 for
  breaking changes.

---

## Email notifications for new episode summaries

**Problem.** Once a daemon is processing your feeds in the background,
the value is in the summaries — but you have to remember to check the
web UI to see what's new. Email delivery flips it: open your inbox in
the morning, see two summaries from yesterday's podcasts, decide
which ones (if any) you want to listen to in full. Push-style instead
of pull.

**Sketch.**

**Trigger.**
- After a `summarize` job succeeds, the worker enqueues a `notify`
  job that depends on it (same dependency mechanism as
  transcribe → summarize). Keeps retry + failure semantics consistent
  and lets email delivery failures retry independently without
  re-running summarization.
- Add `notify` to the worker's dispatch in `_dispatch` calling a new
  `notify_episode(conn, cfg, episode_id)` in `process.py`.

**Content + format.**
- HTML email rendered from a new Jinja template
  (`web/templates/emails/episode.html`) — reuses the existing FastAPI
  Jinja environment so the email view shares helpers with the web
  pages.
- Includes: podcast artwork (when entry #2 lands), episode title,
  link back to the full page on the homelab deploy, the
  3-paragraph `summary`, the top N insights, and chapter titles.
  Skip speaker takes — they're often opinionated and noisy for an
  inbox skim; include a "read more on podracer" link to the full
  page.
- Plain-text fallback generated by stripping the template (`html2text`
  or a simple sibling template) — keeps it readable in terminals and
  improves deliverability.

**Transport.**
- Backend dispatcher mirroring the summarize/transcribe pattern:
  - `smtp` — bring-your-own SMTP server (Postfix on the homelab, a
    workstation's local relay, or msmtp via a public provider). Just
    needs host/port/user/pass.
  - `resend` / `postmark` / `sendgrid` — HTTP API providers, useful if
    you don't want to run SMTP. Resend is cheapest at personal scale
    (3k emails/month free).
- Config:
  ```toml
  [notify]
  backend = "smtp"          # or "resend"
  from_addr = "podracer@jjcloud.net"
  to_addrs  = ["me@example.com"]
  # SMTP-specific:
  smtp_host = "smtp.fastmail.com"
  smtp_port = 587
  # Provider-specific tokens in .credentials/
  ```

**Per-subscription preferences (v2).**
- `podcasts.notify` boolean column with a per-podcast toggle on the
  podcast detail page. Default-on for new subscriptions, easy to
  silence the noisy ones.
- Schema: another idempotent ALTER in `db/connection.py::_migrate`.
- The `notify` job is only enqueued when the source podcast has
  `notify = 1`.

**Digest mode (v3, optional).**
- Instead of one email per episode, an aggregated "last 24 hours"
  email at a fixed local time. Driven by a separate cron-style
  iteration in the worker that gathers episodes summarized since
  the last digest send and emits one combined message.
- Config: `[notify] mode = "immediate" | "digest_daily"` +
  `digest_time = "08:00"`.

**Open questions.**
- Failure mode: if the email backend is misconfigured, the notify
  job retries up to `max_attempts` then goes to `failed`. The summary
  + transcript are already saved, so this is recoverable — fix the
  config, hit Retry from the `/jobs` page.
- Pairing with episode artwork: emails look much better with the
  podcast image inline. Don't gate on entry #2 though — render
  without if the URL is missing.
- Unsubscribe link in the email: not needed at personal scale; if
  ever made public, must do this properly per CAN-SPAM and inbox
  provider reputation rules.
- The notification feature is partially redundant with the planned
  agentic chat — a daily "what's new in my archive?" agent query
  could replace this. Email still wins for ambient/push UX though
  (you don't have to ask).

---

## Filtering + pagination across podcasts, episodes, and jobs

**Problem.** The list views work fine while the archive is small but
break down as it grows. The episodes page already silently caps at
~20 rows; the `/jobs` page shows the top N per status and hides the
rest; subscribed-podcasts listing has no filter at all. Once a few
podcasts get backfilled, you can't find anything without grepping
the SQLite file directly.

**Sketch.** Solve three list pages with the same pattern + shared
helpers.

**Filters per page.**
- **Podcasts list (`/podcasts`):** title substring filter (already
  exists in episodes search — reuse the htmx-style live filter input),
  toggle for subscribed-only vs all. Sort by title, last_synced_at, or
  episode count.
- **Episodes list (`/podcasts/{id}` and any cross-podcast view):**
  - title substring (already partially there as `--search`)
  - status filter (`pending` / `downloaded` / `transcribed` /
    `summarized`)
  - has-transcript / has-summary booleans (different from `status`
    in edge cases)
  - date range on `published_at`
  - speaker filter (when summaries exist — match against the speaker
    list joined off `summaries.data` JSON, or denormalize a
    `speakers(episode_id, name)` table)
- **Jobs (`/jobs`):**
  - status filter (queued/running/done/failed/blocked) — currently
    sectioned by status, but you can't see "all jobs for episode X"
    or "all failed transcribes from yesterday"
  - kind filter (transcribe vs summarize)
  - podcast filter
  - date range on created_at / finished_at

**Pagination.**
- Offset-based for simplicity: `?page=N&per_page=M` with a default
  `per_page = 50`, hard cap 500. Render Prev / Next at the bottom +
  a "page N of M" counter. Works for the UI and the planned REST API
  (entry #10) identically.
- Cursor-based is more correct (no drift if rows are inserted
  mid-pagination) but offset is fine for personal scale. Revisit if
  jobs ever rolls past 100k rows.

**DB indexes worth adding.**
- `idx_episodes_published_at_status` — covers the common case of
  "recent episodes by status".
- `idx_episodes_podcast_status` — already partially indexed via
  `UNIQUE(podcast_id, guid)`; check explain plans before adding.
- `idx_jobs_kind_status` — speeds up `/jobs` filtered views.
- The existing `idx_jobs_status_created` covers most current queries
  but won't with a kind filter added.

**Reusable helpers.**
- `db/episodes.py`, `db/podcasts.py`, `db/jobs.py` each grow a
  `list_*` function taking `(filter_dict, limit, offset)` and
  returning `(rows, total_count)`. Routes call them, templates render
  the same way regardless of which page.
- One shared `pagination_block.html` template partial used by all
  list pages.

**UI: htmx-driven, no page reloads.**
- htmx is already in `base.html`. Wire filters as
  `hx-get="/podcasts?…" hx-target="#results"` so changing a dropdown
  just swaps the results table without a full reload. Keeps the
  filter UX feeling fast.

**Open questions.**
- Saved filter URLs: easy with offset pagination — the URL params are
  the state. Bookmarkable searches are basically free once the
  filter params are query strings.
- The `/jobs` page meta-refresh fights with htmx partial updates (the
  full-page refresh wipes your filter state). Resolve by either
  dropping meta-refresh on filtered views, or moving the auto-refresh
  to htmx polling on the counts block only.
- A "cross-podcast episodes" view (everything queued/done in the last
  week regardless of podcast) doesn't exist today and would
  benefit. Could be its own page or just the episodes filter applied
  with `podcast_id=any`.

---

## In-app notifications sidebar (unread episodes)

**Problem.** The daemon will be summarizing episodes 24/7 once it's
running on the homelab. Opening the site, you currently have no idea
which episodes are new since the last time you looked — you have to
scan podcast pages and remember which titles you've seen. An
unread/notification sidebar surfaces "here's what's new" the moment
you open the site, with a count badge so it works even if the
sidebar is collapsed.

**Sketch.**

**Track "viewed" per episode.**
- Schema: `episodes.viewed_at TEXT` — NULL until the user opens the
  episode detail page. Idempotent ALTER in
  `db/connection.py::_migrate`.
- Set in `episode_detail` route: on every GET, if `viewed_at IS NULL`
  set it to `datetime('now')`. First open marks it read; subsequent
  visits are no-ops.
- An episode is "unread" when `viewed_at IS NULL` AND a summary
  exists (no point showing unread for episodes still processing).

**Sidebar UI.**
- Slide-in panel from the right edge, triggered by a bell icon in the
  nav bar (`base.html`). htmx loads the panel content from
  `GET /notifications` so it's lazy and stays fresh on each open.
- Content: N most recent unread episodes (default 20), grouped by
  podcast. Each row: podcast artwork + episode title + published date
  + first sentence of the summary. Click → episode page (which
  auto-marks read).
- Bell shows a small count badge — number of unread episodes. Updates
  on page navigation (rendered server-side from the same query).
  Zero unread → no badge.
- "Mark all read" button at the top of the panel: `POST
  /notifications/mark-all-read` updates every NULL `viewed_at` to
  now. Useful for clearing a backlog you don't want to actually open.

**Filtering.**
- Optional per-podcast filter inside the sidebar (dropdown). Lets you
  see what's new from Odd Lots specifically without seeing Macro
  Voices noise.
- "Show all" vs "Unread only" toggle so you can flip between
  notifications mode and recent-episodes browse.

**Cross-podcast feed page.**
- Promote the same query to its own page: `/feed` shows everything
  recent regardless of podcast (read or unread), filterable + sorted
  by published_at. Natural pair with the sidebar — sidebar for
  glanceable unread, page for browsing the firehose.

**Interaction with email notifications (#11).**
- Both features rely on the same "episode just finished summarizing"
  event. Different transports — email pushes out, the sidebar pulls
  in. No conflict.
- Per-podcast `notify` flag (from the email feature) could be reused
  to mean "include in unread sidebar". Or keep them separate — some
  podcasts you might want in-app but not email.

**Open questions.**
- Multi-device read state: today it's just one browser, so any
  cookie/session-level "read" state would be fragile. DB-backed
  `viewed_at` is correct even at single-user scale — survives
  switching devices.
- "Star" / "save for later" alongside read state? Probably yes,
  cheap to add (`starred_at TEXT`), but defer until there's a real
  need to triage.
- Time-to-read estimate per episode (transcript length × wpm) shown
  on the sidebar row? Nice-to-have, falls out of episode metadata
  for free.

---

## Authentication + multi-user

**Problem.** The whole app assumes a single user — one DB, no login,
LAN trust. Adding even one more user (e.g. a partner who wants their
own subscriptions on the same homelab instance) requires authentication,
per-user state, and a decision about what's shared vs isolated.

**Sketch.**

**What's shared vs scoped per user.**

| Table / concept | Shared across users | Scoped to user |
|---|---|---|
| podcasts (catalog) | ✓ — same RSS feed = same row | |
| transcripts | ✓ — expensive to compute, no reason to dupe | |
| summaries | ✓ (v1) — same per-podcast instructions assumed | |
| episodes | ✓ — metadata is feed-driven | |
| **subscriptions** | | ✓ — Megan subscribes to Odd Lots, partner doesn't |
| **viewed_at / starred_at** | | ✓ — per-user read state |
| **notification prefs** | | ✓ — different inboxes |
| **custom summarize instructions** | | ✓ (v2) — eventually per-user, see below |
| **conversations / chat history** | | ✓ — private |
| **jobs** | ✓ — work queue is shared, anyone who subscribes triggers work | |

**Schema sketch.**
- New `users(id, email, password_hash, name, created_at)` table.
- `subscriptions(user_id, podcast_id, subscribed_at, notify)`
  replaces the existing `podcasts.subscribed` + `podcasts.subscribed_at`
  columns. The per-podcast watermark becomes per-user-per-podcast.
- New `episode_views(user_id, episode_id, viewed_at, starred_at)`
  replaces the `episodes.viewed_at` from entry #12.
- Existing tables (`podcasts`, `episodes`, `transcripts`, `summaries`,
  `jobs`) stay user-agnostic.
- `find_new_episodes` query becomes: episodes where at least one user
  is subscribed AND that user's per-(user, podcast) watermark is older
  than the episode's created_at, AND no active job exists. The result
  is still "one job per episode" (work shared) but each user sees the
  episode appear in their feed.

**Auth mechanism — three options, ordered by my preference for a
homelab:**

1. **Trust a reverse-proxy header** (recommended).
   - Run [Authelia](https://www.authelia.com/) /
     [Pocket-ID](https://github.com/stonith404/pocket-id) /
     `oauth2-proxy` / Tailscale / Caddy `forward_auth` in front of
     podracer. The proxy authenticates and injects
     `Remote-User: jeremiah@…` into the request headers.
   - Podracer reads that header and either finds an existing user row
     or auto-provisions one.
   - Pro: zero auth code in podracer. The hard part (TOTP, passwords,
     password resets, brute-force protection, etc.) lives in a
     purpose-built tool. Easy to add more apps later under the same
     SSO.
   - Config: `[auth] mode = "proxy"`, `header = "Remote-User"`,
     `trusted_proxy_cidrs = ["10.0.0.0/24"]` (refuse the header from
     untrusted sources).
2. **Built-in password login.**
   - Argon2 password hashing, FastAPI session cookies via
     `starlette.middleware.sessions`. `/login`, `/logout`,
     `/account/password` routes. CLI helper:
     `podracer user add <email>`.
   - Pro: self-contained, no proxy required.
   - Con: now you're maintaining auth code (rate limiting, CSRF,
     reset flow). Real but manageable.
3. **Magic links over email.**
   - Pairs with the email notifications entry — same SMTP stack.
   - Lower friction (no password), still secure if the email account
     is. But requires the email pipe to actually work end-to-end.

Recommendation: do (1) as the v1 — defer auth complexity to a
purpose-built tool — and add (2) only if you ever want to deploy
podracer somewhere without a proxy.

**API + tokens.**
- The REST API (entry #10) already needs auth. With multi-user, API
  tokens become per-user. New table:
  `api_tokens(id, user_id, name, hash, last_used_at, created_at)`.
- Tokens are scoped to a user — calling `POST /api/v1/podcasts` with
  someone else's token subscribes them, not you.

**Migration from single-user.**
- Migration creates `users` and inserts a single row from the existing
  config / env (e.g. read from `[users] bootstrap_email = "..."` if set,
  otherwise prompt on CLI).
- Existing `podcasts.subscribed = 1` rows get migrated into
  `subscriptions(user_id=1, podcast_id=...)` and the column gets
  dropped (or kept as a generated view for backwards compat for one
  release).
- `episodes.viewed_at` → `episode_views(user_id=1, episode_id=...)`,
  same shape.
- Should be a one-shot, idempotent migration that runs at
  `init_db()` time like the rest.

**UI changes.**
- Nav shows the logged-in user + a logout link.
- Every list query gets a "for this user" filter applied
  transparently in the route layer.
- A `/users` page (admin-only) for adding/removing users when running
  proxy auth and you want to limit who can self-provision.

**Open questions.**
- v1 custom summarize instructions (entry #4) — are those per-user or
  shared per podcast? Probably per-user — Megan's "financial trade
  ideas" extraction isn't what her partner wants from the same show.
  Punt to v2; v1 ships with shared instructions and the user override
  comes later.
- Job authorship in `/jobs`: do you see who enqueued a job? Useful
  for debugging shared work. Add `jobs.enqueued_by_user_id` (nullable)
  as a denormalized hint.
- The chat history table (entry #5) needs `user_id` from day one —
  chats are private even in a household. Easy if the chat feature
  lands after this one; if it lands first, retroactive `user_id`
  column with a one-time backfill works.
- Single-user mode should remain the default for the public repo —
  most people running podracer are running it for themselves. Auth
  is opt-in via config (`[auth] mode = "none"` is the default).

---

## Inline audio player synced to transcript + chapters

**Problem.** The summary tells you what was said, but the moment you
think "wait, I want to hear that exactly" you're stuck — there's no
way to listen from the right spot. This feature is the keystone that
turns every timestamped artifact (chapters, insights, speaker takes,
chat citations, search results) into a clickable jump point.

**Sketch.**
- Native `<audio controls preload="metadata">` element pinned at the
  bottom of every episode page (or sticky in a sidebar). Source =
  the downloaded MP3 served from a new
  `GET /media/{podcast_slug}/{episode_filename}` route under
  FastAPI's StaticFiles, scoped to `cfg.media_dir`.
- Every timestamp in the page (`[01:32:14]` in chapters, insights,
  speaker takes, chat citations) becomes an `<a
  data-ts="5534">[01:32:14]</a>`. Small JS handler: on click, set
  `player.currentTime = ts` and play. Update a `#now-playing`
  badge with the current chapter title (derived from
  `summary.chapters` + `audio.currentTime`).
- Resume position: persist `audio.currentTime` to localStorage keyed
  by episode id every few seconds; restore on next visit. Cheap
  "where was I" without server state. Per-user variant once auth
  lands (#14) — `episode_playback(user_id, episode_id, position_sec,
  last_played_at)`.
- Transcript scroll-sync: as the audio plays, highlight the
  utterance whose `[HH:MM:SS]` matches the current time. Implement as
  a sorted array of `(ts, dom_node)` walked by a `timeupdate`
  listener.
- Keyboard shortcuts: space toggles play/pause, `←`/`→` for ±15s,
  `J`/`K`/`L` for ±10s + pause (yt-style).
- Streaming when audio has been pruned (#disk-pruning): if the local
  MP3 is gone, fall back to `episode.audio_url` from the feed.
  Slower but still works.

---

## Cost tracking dashboard

**Problem.** Today you have no idea what podracer is actually
spending on Deepgram + OpenRouter. The auto-backfill incident
(596 jobs queued for $200 of potential spend) was the wake-up call.
A simple dashboard turns spend into a known quantity — green when
it's $5/month, red when it's $200, with breakdowns so you can decide
which subscriptions to silence.

**Sketch.**
- Schema: new `processing_costs(episode_id, job_id, backend, model,
  unit, units, cost_cents, created_at)` table. Each transcribe /
  summarize call inserts a row. `unit` is "audio_seconds" for
  transcribe, "tokens_out" for summarize. `cost_cents` is the
  computed dollars * 100 stored as an integer.
- Backend prices live in a small config-driven table (or just
  hard-coded in `process.py`) — Deepgram nova-3 is
  `$0.0043 / min`, DeepSeek V4 Flash on OpenRouter is
  `$0.14 / 1M in` + `$0.28 / 1M out`, etc. Refresh the table when
  prices change.
- Instrumentation: `_transcribe_deepgram` already gets back audio
  duration in the response payload — log it. `_chat_openrouter`
  gets `usage.prompt_tokens` / `usage.completion_tokens` — log
  both. No SDK changes needed.
- New `/costs` page: this month total, breakdown by podcast,
  breakdown by stage (transcribe vs summarize). A small line chart
  (sparkline ok, Chart.js is overkill) of daily spend. Filterable
  by date range.
- JSON API: `GET /api/v1/costs?since=&group_by=podcast|day` once the
  REST API lands.
- Alert/throttle: optional `[costs] daily_budget_cents = 500` config
  — worker logs a warning when the day's spend crosses the threshold
  and refuses to claim new jobs until midnight. Aggressive default
  is "off"; opt-in when you've been burned once.

---

## Speaker name correction UI

**Problem.** The speaker-ID LLM step is good but not perfect. We hit
"Joe Wazenthal" / "Tracy Allaway" within the first hour of using the
system. Today the fix is editing the JSON in the database manually
or re-running summarize with new prompts and hoping. A simple inline
edit makes corrections a one-click operation that compounds across
future episodes of the same show.

**Sketch.**
- On the episode page's Speakers table, each row gets an "edit"
  affordance (inline contenteditable, or a small modal). Save POSTs
  to `/episodes/{id}/speakers/{label}` with the corrected name.
- Server: load the saved `PodcastSummary`, update the matching
  speaker entry, write the JSON back to `summaries.data`. Single
  row update, no LLM round-trip.
- Persist corrections per podcast so future episodes don't repeat
  the mistake: new `podcasts.speaker_overrides` JSON column,
  `{"SPEAKER_00": "Joe Weisenthal", "SPEAKER_01": "Tracy Alloway"}`
  for shows where the same speakers recur. The speaker-ID prompt
  picks up these as authoritative — feeds them in as part of the
  show notes / podcast description context.
- Display: also let the user merge two SPEAKER labels that the
  diarizer split (we already get this from the LLM merging via
  comma-separated labels, but errors slip through). UI button
  "merge with…" combines two rows and re-runs `rewrite_transcript`
  on the saved summary.
- Pairs with the agentic chat: a "this is wrong, fix it" command
  inside the chat could trigger the same backend.

---

## Quote extraction

**Problem.** Speaker takes capture positions ("she argued X"); they're
useful for summary, not as shareable artifacts. The actual lines you'd
want to tweet, paste in a Slack channel, or quote in a blog post are
verbatim — the memorable sentences. Adding a "quotes" stage to the
summarize pipeline surfaces those automatically.

**Sketch.**
- New 6th LLM pass alongside the existing five (summary, chapters,
  insights, speaker_takes, custom_notes from #4). Prompt asks for
  10–15 verbatim, single-sentence quotes that stand alone — no need
  for surrounding context to land.
- Schema: extend `PodcastSummary`:
  ```python
  class Quote(BaseModel):
      speaker: str
      text: str
      timestamp: str
  class PodcastSummary(BaseModel):
      ...
      quotes: list[Quote]
  ```
- Render on the episode page as a quotes section (block-quote style,
  large text, attribution). Each quote has a copy-to-clipboard button
  and — once the audio player lands — a play-this-quote shortcut.
- Cost: one extra LLM call per episode, ~$0.05 on DeepSeek V4 Flash.
- Feeds into the share-link feature naturally: "share this quote"
  becomes its own surface.

---

## Disk management / audio pruning

**Problem.** Audio files dominate disk usage — ~100 MB per hour of
podcast. At 20 hours/week of new content, you fill 50 GB in ~6
months and 500 GB in ~5 years. The transcript is the durable artifact
(~200 KB per episode); the audio is just a cache that podracer
re-downloads from the feed if needed.

**Sketch.**
- New config: `[storage] keep_audio_days = 30` and `keep_audio = "all"
  | "none" | "starred" | "unviewed"`. Defaults to "all" (no behavior
  change unless you opt in).
- Worker iteration calls a `prune_audio()` step after the drain:
  - For each episode where `local_path IS NOT NULL` AND `status =
    'summarized'` AND `summarized_at < now - keep_audio_days`:
    delete the file, set `local_path = NULL`, set
    `pruned_at = now`.
  - Respect the keep policy: skip starred / unviewed episodes.
- `transcribe_episode` already re-downloads when `local_path` is
  None, so a pruned-and-then-rerun pipeline just works.
- UI: episode page shows a small badge when audio has been pruned.
  Optional "re-download audio" button that fetches from `audio_url`
  on demand (useful when the audio player wants the local file).
- Stretch: total-storage budget instead of age-based — keep the most
  recent ~50 GB of audio regardless of age.

---

## Public read-only share links

**Problem.** Once you have a great summary, the natural next move is
"send this to a friend". Today that means screenshots or copy-paste.
A shareable URL — read-only, no login, doesn't expose the rest of your
archive — turns each summary into a real artifact you can pass around.

**Sketch.**
- Schema: `episode_shares(token, episode_id, created_by_user_id,
  created_at, expires_at, view_count)`. `token` is a 22-char
  url-safe random string (≈128 bits of entropy).
- New routes:
  - `POST /episodes/{id}/share` → create a token, return the URL.
  - `GET /share/{token}` → render a stripped-down version of the
    episode page (no chat, no jobs, no auth required), increment
    `view_count`.
  - `DELETE /share/{token}` → revoke.
  - List + manage at `/account/shares` once auth (#14) is in.
- The rendered page reuses the existing `episode_detail.html`
  template but with a `share_mode = True` flag that hides the
  "Process this episode" button, the chat widget, navigation links
  to other parts of the app, etc.
- Optional expiration: `?expires=7d` on the create endpoint generates
  a token that auto-revokes after N days. Cleanup happens lazily on
  GET (compare `expires_at` to now).
- Stretch — OpenGraph tags so the link previews nicely in
  Slack/iMessage/Twitter: `<meta property="og:title">` with the
  episode title, `og:description` with the first paragraph of the
  summary, `og:image` with the podcast artwork. Cheap and makes the
  link feel like a polished artifact, not just a JSON dump.

---

## GitHub Actions CI

**Problem.** The repo is public and the test suite + lint + typecheck
have been keeping things honest, but right now they only run when I
remember to invoke them locally. A regression slipped into `main`
would land silently. CI moves that from "discipline" to "guarantee".

**Sketch.**
- Single workflow `.github/workflows/ci.yml`, triggers on
  `pull_request` and `push: branches: [main]`.
- Matrix on Python (start with `3.12`; expand to `3.10` / `3.11` /
  `3.13` if anything actually exercises version-specific code).
- Steps:
  1. Checkout
  2. Install `uv` via the official action (`astral-sh/setup-uv@v3`)
  3. `uv sync --extra dev` — slim install, no `whisper` extra. Tests
     mock external calls so torch/whisperx aren't needed.
  4. `uv run ruff check podracer/ tests/`
  5. `uv run ty check podracer/ tests/`
  6. `uv run pytest`
- Cache: `setup-uv` handles the package cache automatically. Builds
  should land in <30 s after the first run.
- Required-checks branch protection on `main`: PRs can't merge until
  CI passes. (Optional now since you're solo on the repo, but cheap
  insurance for the day a contributor PRs in.)
- Add a status badge to `README.md`: a small `![CI](…/badge.svg)`
  next to the title.

**Stretch.**
- Separate job that runs `uv sync --extra whisper --extra dev` on
  ubuntu-cuda (or just ubuntu, since the whisper service imports work
  without a GPU at module load time — only `whisper.load_model`
  needs CUDA). Catches torch/whisperx ABI drift without needing real
  inference.
- A nightly job that runs the full pipeline against a fixed 30-second
  audio fixture via the cloud backends (Deepgram + OpenRouter). Costs
  ~$0.02/run; catches API contract drift before users hit it. Gated
  by `if: github.event_name == 'schedule'`.
- Auto-deploy on green main: a separate workflow that SSHes into the
  homelab LXC and runs `ansible-playbook playbooks/podracer.yml`.
  Requires an SSH key as a GitHub secret. Cheap to add once the
  homelab Ansible role exists.

---

## Public demo instance

**Problem.** The README says what podracer does; a live demo would
let a curious visitor see it in 30 seconds. A read-only public
instance, seeded with a handful of well-known podcasts, makes the
project actually shareable — links to it can go in HN comments, blog
posts, the GitHub repo header, etc.

**Sketch.**
- Separate deploy from the private homelab one — different DB,
  different config, different domain. Could live on a small VPS
  (Hetzner CX11, Vultr $5 box) or as a second LXC behind a public
  reverse proxy (Cloudflare Tunnel from the homelab works without
  exposing your home IP).
- Seed content: 5–10 episodes across 2–3 well-known podcasts that
  don't mind being indexed — Lex Fridman, Huberman, Dwarkesh, Odd
  Lots, EconTalk. Pre-processed once, then locked down (no worker
  running). Total cost: ~$10 of Deepgram + OpenRouter for the seed.
- Lockdown mode: a new `[demo] read_only = true` config flag that
  - disables `POST /search/subscribe`, `POST /episodes/*/enqueue`,
    `POST /episodes/*/chat` (or stubs them with a friendly "this is
    a demo, fork the repo to run your own" message)
  - hides the `/jobs` admin page
  - removes the "process this episode" button from the template
- Banner: a small "This is the podracer demo. Fork
  [github.com/jjmalina/podracer](https://github.com/jjmalina/podracer)
  to run your own instance." across the top of every page when
  `read_only = true`.
- Auth: none. LAN-trust doesn't apply to the internet, so the public
  read-only API (entry #10) lives behind the same flag — GETs work
  for anyone, POSTs return 403 unless authenticated as the demo
  admin.
- Updates: the demo doesn't run the worker. Refresh is manual —
  occasionally re-run the seed script to add new episodes. Keeps the
  cost bounded and removes the "what if the demo accidentally
  transcribes my whole archive at 3 AM" failure mode.
- Stretch — a "try it on your own podcast" form: paste an RSS URL,
  process exactly one episode against a quota'd Deepgram +
  OpenRouter budget, display the result. More compelling than
  pre-seeded content but adds real cost and abuse-surface (rate
  limits, captchas, etc.). Defer until the basic read-only demo is
  out.

**Open questions.**
- Cost ceiling: the demo should have a hard monthly cap on cloud
  spend (e.g. $5). Easiest: don't run any LLM/transcription calls
  at all — all the demo's content is pre-baked and immutable. Save
  the "try on your podcast" flow for v2.
- Domain: subdomain of an existing one (`podracer.jjcloud.net` is
  already taken by the private instance — maybe `demo.podracer.dev`
  or just `podracer.example.com`).
- Trust signal: link from the public demo to the GitHub repo
  (already covered by the banner) plus a clear "do not enter
  personal data" note since chat would be off anyway.

---

## Interactive installer

**Problem.** `scripts/setup.sh` is one-shot and non-interactive: it
runs apt + uv + symlinks, then dumps a banner telling you to drop
API keys in `.credentials/`. Fine for someone who already knows the
project; bad first impression for a fresh user who cloned the repo
and just wants something working in five minutes. An interactive
mode picks up where the headless mode leaves off — prompts for the
two API keys, lets you pick backends, and (optionally) installs the
daemon.

**Sketch.**

- Add a `--interactive` flag (or detect a TTY automatically) on
  `scripts/setup.sh`. Headless behavior stays the default for the
  Ansible-driven LXC deploy — no prompts there.
- Walk the user through, with sensible defaults the user can take by
  hitting Enter:

  ```
  1. Transcription backend:
     [1] Deepgram (cloud — recommended, no GPU)
     [2] Whisperx-http (self-hosted, needs an NVIDIA GPU)
     Choice [1]: _

  2. Summarization backend:
     [1] OpenRouter (cloud — recommended)
     [2] Ollama (local, must be already running)
     [3] vLLM (local, must be already running)
     Choice [1]: _

  3. Deepgram API key (from https://console.deepgram.com/, blank to skip): _
  4. OpenRouter API key (from https://openrouter.ai/keys, blank to skip): _

  5. Install as a systemd --user service so it runs in the background? [y/N]: _

  6. Subscribe to a podcast to test? (paste an RSS feed URL, blank to skip): _
  ```
- After answers:
  - Writes `config.toml` (or `~/.config/podracer/config.toml` if
    `--daemon` was chosen) with the chosen backends.
  - Writes any provided keys to `.credentials/{deepgram_token,
    openrouter_token}` with `chmod 600`.
  - If "install daemon" was yes, calls
    `scripts/install-systemd-user.sh`.
  - If a feed URL was given, runs `podracer subscribe <url>` so the
    user sees something happen immediately. With auto-queue (already
    landed), the latest episode starts processing within ~10 seconds.
  - Prints a final "open http://localhost:8080" line.
- All prompts use `read -p` / `read -s` (for keys) — pure bash, no
  Python TUI library required.
- Validate inputs as you go: empty key → skip + warn; bogus URL →
  re-prompt; can't subscribe → continue with a note.

**Tests.**
- Expect script via `expect(1)` or python `pexpect` to drive a
  scripted run end-to-end against a fresh container. Confirms the
  banner, the prompt order, and that the final state matches
  (config file + symlinks + systemd units, if requested).
- Not gating CI on this initially — interactive flows are flaky to
  test. Document it as a manual smoke test before tagging a release.

**Stretch.**
- A Python TUI version (Textual / questionary) for the same flow.
  Prettier, supports arrow-key selection. Adds a dependency for what
  is otherwise a pure-bash script — only worth it if installs grow
  to enough steps that the bash version starts feeling clunky.
- "Doctor" subcommand — `podracer doctor` — that checks an existing
  install: are credentials present? Does Deepgram return 200 on a
  trivial probe? Is OpenRouter reachable? Is the venv healthy?
  Catches misconfigurations after the fact.
