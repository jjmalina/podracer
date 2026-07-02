# Daily & weekly digests

**Date:** 2026-06-24
**Status:** Planned
**Depends on:** existing summarize pipeline (`summarize.py`), worker (`worker.py`), JSON API (`web/routes/api.py`)

## Goal

A **digest** is a summary of summaries: a one-line roundup of every episode from
a given day (or week) — dated by its publish/sync recency — **grouped by topic,
then by show**, under a short LLM-written overview. One digest per day, one per
week. Grouping by topic (instead of leaving 20+ shows scattered) is what makes a
busy day — and especially a *week* — skimmable: you read the "Technology"
section, then "Business," rather than show by show. They render as a
skimmable reverse-chronological **feed of digests** at `/digests`, so you can
catch up on the archive at a glance instead of scrolling the episode feed.

The digest is a genuine *summary of summaries*: a cheap LLM pass reads the day's
**stored episode summaries** (never transcripts) and writes the overview + the
one-liners. The structure (topic → show → episode) is assembled deterministically
in Python from the shows' tags; the model only does language.

## Decisions locked in

These were settled up front; the rest of the plan builds on them.

1. **Dating — by recency (publish time, else sync time).** An episode belongs to
   the day/week containing its `COALESCE(published_at, created_at)` — the show's
   own publish time when the feed provides one, else first-seen/sync time —
   interpreted in `digest_timezone`. This is *exactly* the recency key the home
   feed already sorts by (`idx_episodes_recency`), so a digest's "this day"
   matches the feed's, and it's null-safe. **Deliberately not
   `summaries.created_at`:** processing lag must not decide an episode's date.
   Consequence: a digest is **not strictly immutable** — an episode summarized
   after its day has closed is folded in by regeneration (see Scheduling), not
   silently lost.
2. **Style — LLM-compiled one-liner roundup, grouped topic → show.** Each
   episode gets one model-written sentence; lines are grouped **by topic, then by
   show within the topic**, so related episodes across shows cluster instead of
   scattering (matters most on the weekly). A short model-written `overview` sits
   at the top. No free-form cross-show thematic *prose* in v1 — the topic
   grouping is deterministic from the shows' tags; the model only writes the
   one-liners and the overview.
3. **Trigger — scheduled finalize + manual.** The worker finalizes the
   *previous* period once the local clock passes `digest_hour` (e.g. 08:00).
   `podracer digest` (CLI) and a web "regenerate" button cover on-demand and
   backfill. The CLI is the primary interface; the worker calls the same code
   path.
4. **Timezone — explicit config.** A `digest_timezone` (IANA name) plus
   `digest_hour` decide period boundaries and when finalize fires.

## Timing model

Let `tz = digest_timezone`, `H = digest_hour` (local hour, default 8).

- **Daily.** Day `D` (a calendar date in `tz`) is *finalizable* at `H:00` local
  on `D+1`. At that point the worker generates `D`'s digest over every
  **summarized** episode whose `COALESCE(published_at, created_at)` falls in the
  local-day window `[D 00:00, D+1 00:00)`. If a straggler for `D` is summarized
  later, the next scheduler tick regenerates `D` (the membership-grew check
  under Scheduling) — generation is idempotent.
- **Weekly.** Week `W` (ISO week, **Mon–Sun**, in `tz`) is finalizable at `H:00`
  local on the Monday after it ends. Generated **hierarchically from that week's
  daily digests** when present (cheaper, can't contradict the dailies), falling
  back to the raw episode summaries for any day without a daily digest.
- **Why 8am, not midnight.** Gives late-night processing time to settle, and the
  digest is freshly waiting when you wake up. The hour is config, so this is a
  knob.
- **No live "today so far."** The current, still-open day has no digest until it
  closes. Simpler, and matches the "catch up in the morning" use case. A live
  partial is possible later but explicitly out of scope for v1.

### Backfill watermark

On first run the scheduler records a `digest_watermark` (today's local date) in
`config`, mirroring the `worker_watermark` / `subscribed_at` pattern. **Auto
generation only covers days on/after the watermark** — it will not silently
synthesize a digest for every historical day in the archive. Backfilling older
periods is a deliberate CLI op (`podracer digest --backfill A..B`).

## Data model

### Schema (add to `db/connection.py` `SCHEMA`)

```sql
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- 'day' | 'week'
    period_start TEXT NOT NULL,             -- local date 'YYYY-MM-DD' (week = its Monday)
    period_end  TEXT NOT NULL,              -- exclusive local date bound
    data        TEXT NOT NULL,              -- DigestData JSON (see below)
    episode_count INTEGER NOT NULL,
    model       TEXT NOT NULL,
    backend     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(kind, period_start)
);
```

`UNIQUE(kind, period_start)` makes regeneration an upsert (`ON CONFLICT … DO
UPDATE`), exactly like `save_summary`. No new index needed: membership filters on
`COALESCE(published_at, created_at)`, which the existing
`idx_episodes_recency` expression index already covers.

### Stored shape (`DigestData`)

Pydantic models in a new `podracer/digest.py`. The LLM emits only `overview` +
flat `items` (one per episode); Python joins episode/show/**topic** metadata and
builds the topic → show → episode tree, then stores the **assembled snapshot** so
the web/API never re-join:

```python
class DigestItem(BaseModel):          # what the LLM returns, per episode
    episode_id: int
    one_liner: str

class DigestLLMOutput(BaseModel):     # the model's structured response
    overview: str
    items: list[DigestItem]

class DigestEpisode(BaseModel):       # assembled, stored
    episode_id: int
    title: str                        # snapshot at generation time
    one_liner: str

class DigestShow(BaseModel):
    podcast_id: int
    podcast_title: str
    episodes: list[DigestEpisode]

class DigestTopic(BaseModel):
    topic: str                        # e.g. 'Technology'; 'Other' when untagged
    shows: list[DigestShow]
    episode_count: int                # episodes under this topic

class DigestData(BaseModel):
    overview: str
    topics: list[DigestTopic]         # topic → show → episode
    episode_count: int                # DISTINCT episodes across the period
```

Topic order = `episode_count` desc, `'Other'` last; shows within a topic =
episode count desc then title; episodes = recency desc. Storing a denormalized
title/show snapshot matches the digest's nature ("what the archive looked like
that morning"); `episode_id` is always kept so links resolve even if a title
later changes.

**Placing a multi-topic show.** Topics are per-*show* tags (`podcast_tags`), and
a show can carry several. Two choices:
- **v1 (recommended): list the show under *each* of its topics.** No hidden
  episodes, no arbitrary "primary," and zero tagging-schema change. The same
  episode one-liner can appear in two topic sections; the digest-level
  `episode_count` counts *distinct* episodes so totals don't inflate.
- **Fast-follow: one primary topic per show.** Cleaner (no repeats), but needs a
  meaningful primary — and today `_attach_topics` returns tags *alphabetically*
  (`ORDER BY t.name`), with no feed-order column on `podcast_tags`, so `topics[0]`
  is arbitrary (Business before Technology). Doing this right means preserving the
  feed's `<itunes:category>` order (add a `position` column to `podcast_tags`,
  set it in `set_podcast_tags`, order `_attach_topics` by it) so `topics[0]` is
  the show's declared primary category. The `DigestData` tree is identical either
  way — this is a placement rule, not a model change, so v1 can ship and upgrade
  later.

## Membership query

The boundary is a *local* day, but the stored timestamps are not. Compute the
UTC instants for the local window with `zoneinfo` (stdlib, DST-correct) and query
a half-open range:

```python
from datetime import datetime, time
from zoneinfo import ZoneInfo

def utc_bounds(period_start: date, period_end: date, tz: str) -> tuple[str, str]:
    z = ZoneInfo(tz)
    lo = datetime.combine(period_start, time.min, z).astimezone(UTC)
    hi = datetime.combine(period_end,   time.min, z).astimezone(UTC)
    fmt = "%Y-%m-%d %H:%M:%S"                      # matches datetime('now')
    return lo.strftime(fmt), hi.strftime(fmt)
```

```sql
SELECT e.id AS episode_id, s.data AS summary_data,
       e.title, e.podcast_id, p.title AS podcast_title
FROM episodes e
JOIN summaries s ON s.episode_id = e.id            -- only summarized episodes are members
JOIN podcasts p ON p.id = e.podcast_id
WHERE COALESCE(e.published_at, e.created_at) >= ?  -- UTC window bounds
  AND COALESCE(e.published_at, e.created_at) <  ?
  AND p.subscribed = 1                             -- subscribed shows only, like the feed
ORDER BY p.title, COALESCE(e.published_at, e.created_at) DESC;
```

> `published_at` is feed-local ISO (`2026-06-15T14:30:00`) while `created_at` is
> UTC (`2026-06-15 14:30:00`); like the feed's `relative_time`, we treat both as
> UTC — the sub-day error on feed-local publish times is immaterial at a
> day-bucket boundary (the date portion dominates the lexical range compare).
> The `JOIN summaries` is what makes membership "summarized episodes," so an
> episode with no summary yet is simply not a member — and folds in later via the
> straggler regen.

Each member is then enriched with its show's **topics** (batch-load the distinct
`podcast_id`s with the same `podcast_tags`→`tags` lookup `_attach_topics` uses)
so generation can build the topic → show → episode tree.

## Generation (`podracer/digest.py`)

Reuses the summarize machinery directly — `Backend`, `_checked_or_fail`, the
degenerate-output retry/validation loop, structured-JSON schema enforcement,
token-usage logging. No new LLM plumbing.

```python
def generate_digest(
    members: list[DigestMember], *, backend: Backend, kind: str,
) -> DigestData:
    # members carry {episode_id, podcast_title, title, topics, summary_prose, top_highlights}
    # 1. Build the user message: per-episode blocks (show — title — summary +
    #    a few highlights). Compact; this is summaries, not transcripts.
    # 2. _checked_or_fail(DigestLLMOutput, backend, DIGEST_PROMPT, user, _check_digest)
    #    _check_digest: one_liner present & non-stub for every member episode_id,
    #    overview >= N chars, no episode dropped/hallucinated.
    # 3. Join member metadata; build the topic → show → episode tree (a show
    #    appears under each of its topics, 'Other' when untagged); order as above.
    return DigestData(...)
```

- **Prompt.** "You are compiling a daily/weekly digest. For each episode write
  one tight, standalone sentence — the single thing worth knowing. Then write a
  1–2 sentence overview of the day across all shows. Do not invent episodes; use
  exactly the ones provided." Schema-constrained output keyed by `episode_id`.
- **Weekly = hierarchical.** Feed the week's **daily digests** (overview +
  one-liners) as input instead of re-reading every summary; fall back to raw
  summaries for days that have no daily digest. Keeps weekly input ~10× smaller
  and consistent with the dailies.
- **Empty period → no row.** If `members` is empty, skip; the feed renders
  nothing for that day. Never ask the model to summarize nothing.
- **Save.** `save_digest(conn, kind, period_start, period_end, data, model,
  backend)` upserts on `(kind, period_start)`.

DB helpers live in a new `podracer/db/digests.py` (`save_digest`, `get_digest`,
`get_digests` paginated, `digest_exists`, plus the membership query), re-exported
from `db/__init__.py` like every other table module.

## Scheduling (worker)

Add `_schedule_digests()` to the `Worker` loop, called each drain iteration (two
cheap indexed queries + a tz comparison; short-circuits before `digest_hour`).

```python
def _schedule_digests(self) -> None:
    if not self.cfg.digest_enabled:
        return
    init_digest_watermark(self.conn)                 # set to today (local) once
    for period in due_periods(self.conn, self.cfg):  # daily + weekly, since watermark
        try:
            generate_and_save(self.conn, self.cfg, period)
            logger.info("digest_generated", kind=period.kind, start=period.start)
        except Exception:
            logger.exception("digest_failed", kind=period.kind, start=period.start)
            sentry_sdk.capture_exception()
            # left un-generated → still "due" next tick → self-healing retry
```

`due_periods` returns periods that are finalizable now (given `tz`/`H`) and
on/after the watermark, and that **either** have no row **or** are stale — where
*stale* = the live membership count (the cheap `COUNT(*)` form of the membership
query) exceeds the stored `episode_count`. The stale check is bounded to a recent
horizon (e.g. the last ~14 days) so the scheduler never recounts all history each
tick. This is what makes dating-by-recency safe despite losing strict
immutability: a straggler summarized after its day closed grows the count, the
day goes stale, and the next tick regenerates it. (A closed period whose count is
unchanged is skipped — so steady state is one cheap count query per recent day.)

Generation is inline (like `_sync_feeds`), not an episode `jobs` row — the jobs
table is `episode_id NOT NULL` and episode-scoped, and shoehorning a non-episode
kind in needs a core migration. Inline + "still due ⇒ retried next tick" gives
retry/idempotency without that risk. (If we later want digest runs on the
`/jobs` page, generalize the jobs table then.)

## CLI (`podracer digest`)

Primary interface; the worker calls the same `generate_and_save`.

```
podracer digest                      # generate any due-but-missing periods now
podracer digest --date 2026-06-23    # (re)generate that day
podracer digest --week 2026-06-15    # (re)generate that ISO week (Mon date)
podracer digest --backfill 2026-06-01..2026-06-23   # generate a date range
podracer digest --force              # regenerate even if a row exists
podracer digest --show 2026-06-23    # print a stored digest (no LLM)
--json                               # machine-readable, for agents
```

## Web

- **Nav.** Add `Digests` to `base.html` (Feed · Digests · Podcasts · Search ·
  Jobs).
- **`/digests` — the feed.** Reverse-chronological cards, a `day|week` toggle
  (querystring, mirroring the feed's status chips). Each card: period label
  (`Tue · Jun 23` / `Week of Jun 16–22`), episode count, the `overview` line,
  and the busiest topic or two as a teaser. Paginated like the feed (`PAGE_SIZE`).
- **`/digests/{kind}/{period_start}` — detail.** Full `overview`, then **topic
  sections**, each with its shows, each episode a one-liner linking to
  `/episodes/{id}`. A POST `…/regenerate` button (admin action, like
  `resummarize`).
- **Design.** Instrument-panel aesthetic, amber accent, `eyebrow` + card
  patterns already in `feed/list.html`; new partials under
  `templates/digests/`. Link-back to source episodes is load-bearing — the
  digest points *into* the archive.
- Route module `web/routes/digests.py`, registered in `web/app.py`; reuse
  `relative_time` / `_format_duration` helpers.

## API

Extends the read-only `/api/v1` surface (the api.py docstring already names "an
aggregator that digests a topic" as a target consumer):

```
GET /api/v1/digests?kind=day&limit=&offset=     -> DigestPage
GET /api/v1/digests/{kind}/{period_start}        -> ApiDigest (404 if absent)
```

`ApiDigest` mirrors `DigestData` (overview, topics→shows→episodes→one_liner,
episode_count, period_start/end, model). Purpose-built response models, same as
the existing API.

## Config (`config.py` + `[digest]` table in config.toml)

```python
# Digests
digest_enabled: bool = True
digest_timezone: str = "UTC"      # IANA, e.g. "America/New_York" — SET THIS
digest_hour: int = 8              # local hour to finalize the previous period
digest_week_start: int = 0        # 0 = Monday (ISO)
# digest LLM defaults to the summarize backend/model unless overridden:
digest_backend: str | None = None
digest_model: str | None = None
```

Loaded under a `[digest]` section in `load_config`, same pattern as
`[summarize]`/`[daemon]`. Default `digest_timezone="UTC"` is safe but should be
set per deployment (the homelab LXC runs UTC; the user is Eastern).

## Migrations

`SCHEMA` is `CREATE TABLE/INDEX IF NOT EXISTS`, so the new table + index apply on
startup with no `_migrate` change. (`_migrate` is only for `ALTER`-ing existing
tables.) `init_db` already runs `executescript(SCHEMA)`.

## Phasing

1. **Substrate + manual generation (dogfood).** Schema, `digest.py`,
   `db/digests.py`, membership query, `podracer digest` CLI. Generate a few real
   days by hand, eyeball quality, tune the prompt. *This is the cheapest way to
   judge whether LLM-compiled one-liners actually beat clipped text.*
2. **Worker scheduler.** `digest_enabled`, watermark, `_schedule_digests`,
   `due_periods`, config wiring. Now it's automatic.
3. **Web.** `/digests` feed + detail + nav + regenerate button.
4. **API.** `/api/v1/digests` endpoints.
5. **Weekly hierarchical** pass (can ship in step 1 as flat, upgrade here).

Each step: `ruff check` + `ty check` clean, tests for the membership window
(DST boundary!), the local→UTC bounds, idempotent upsert, and empty-period skip.

## Settled

- **Grouping:** topic → show → episode (the one-liner roundup, organized by
  topic). Matters most on the weekly.
- **Week boundary:** ISO **Mon–Sun** (`digest_week_start=0`).
- **Scope:** subscribed shows only (matches the feed).
- **Weekly input:** hierarchical from the dailies, raw-summary fallback per
  empty day.
- **Dating:** `COALESCE(published_at, created_at)`.
- **Immutability is explicitly a non-goal.** Lag is typically small; the
  straggler-regen folds late episodes into a recent day. No freezing.

## Open questions

- **Multi-topic placement.** v1 lists a show under *each* of its topics (no
  hidden episodes, no tagging change; distinct `episode_count`). Fast-follow is
  one primary topic per show, which needs feed-order tag preservation (a
  `position` column on `podcast_tags`) to be meaningful. Flag if the v1
  duplication reads badly and we should do the primary-topic work up front.
- **Cost.** Negligible — input is stored summaries, not transcripts. A busy day
  (~10 episodes) is well under context; weekly is hierarchical. Log token usage
  (the summarize path already does) to confirm.
- **Backlog before the watermark.** Episodes whose recency predates
  `digest_watermark` never get an auto digest; reachable only via `--backfill`.
  Fine for v1.
```
