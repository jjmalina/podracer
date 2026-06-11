# TODO — fixes & tech debt

Findings from a full code review (2026-06-10). Unlike `roadmap.md`
(future features), these are corrections to existing code. Ordered by
priority within each section. Check items off / delete them as they land.

## High priority

- [x] **Per-request DB connections in the web app** — `web/app.py:59`
  creates one `sqlite3.connect(..., check_same_thread=False)` shared
  across all requests. Routes are sync `def`s, so FastAPI runs them in a
  threadpool: multiple threads use the same connection concurrently.
  Modern CPython sqlite3 is serialized so it won't corrupt the DB, but
  interleaved transactions and `database is locked` errors are likely
  once the worker + multiple browser tabs write at once. Fix: a
  per-request connection dependency (cheap with SQLite/WAL), or a
  thread-local connection helper.

- [ ] **SSRF via unvalidated `feed_url`** — `web/routes/search.py:39`
  (`GET /search/browse?feed_url=...`) fetches any URL. On a LAN this
  turns the web UI into a proxy into internal services (Proxmox API,
  etc.). Fix: require http/https scheme and reject private-range /
  loopback hosts before fetching. Same check applies to the subscribe
  path.

- [ ] **Atomic artifact save + status update** — `db/summaries.py` and
  `db/transcripts.py` write the artifact, update `episodes.status`, and
  re-query the ID as separate statements. A crash in between leaves
  status drifted from reality. Fix: wrap in one transaction; return the
  ID from the insert (`RETURNING` or `cursor.lastrowid`) instead of
  re-querying.

- [ ] **Worker feed-sync commit ordering** — `worker.py` (~line 90): if
  episode upserts succeed but `update_podcast_synced` fails, the
  watermark doesn't advance and the same episodes are re-fetched next
  cycle. Fix: commit per-podcast only after the watermark update
  succeeds; treat each podcast's sync as one transaction.

- [ ] **Timing-safe token compare in whisper service** —
  `whisper_service/routes.py:28` uses `token != state.auth_token`.
  Replace with `hmac.compare_digest(token, state.auth_token)`. While
  here: log auth failures (currently silent).

## Medium priority

- [ ] **Worker loop tests** — `podracer/worker.py` has zero coverage:
  `run_once()`/`run_forever()`, signal handling, feed-sync exception
  recovery, watermark advancement. The queue internals are well tested;
  the daemon loop around them is blind. Target: `tests/test_worker.py`
  with mocked feed/transcribe/summarize backends (~100–150 lines).

- [ ] **CLI tests** — `podracer/cli.py` (20+ commands) is untested: flag
  parsing, config loading, error paths. Target: `tests/test_cli.py`
  against a temp DB.

- [ ] **systemd unit drift from deploy plan** — `deploy/systemd/*.service`
  hardcode `%h/code/podracer/.venv` and lack the
  `Environment=PODRACER_DB=...` / `PODRACER_MEDIA_DIR=...` lines the
  homelab plan (`docs/plans/2026-05-18-homelab-deploy.md`) specifies.
  Also add `StartLimitBurst=5` + `StartLimitIntervalSec=300` to the
  worker unit so one malformed RSS feed can't crashloop it forever.

- [ ] **`/health` endpoint** — the web app has no healthcheck route.
  Add `GET /health` → `{"status": "ok"}` (optionally ping the DB) for
  systemd / reverse-proxy / Prometheus checks on the homelab deploy.

- [ ] **Unify retry policy across backends** — transcribe retries
  ConnectError/ReadTimeout only; OpenRouter retries 429 only (and
  ignores `Retry-After`); ollama/vLLM calls have no retry at all. Pull
  the tenacity policy into one place, retry transient 5xx, respect
  `Retry-After` on 429.

## Low priority

- [ ] **Parameterize `LIMIT`** — `db/episodes.py:31` builds
  `LIMIT {int(limit)}` via f-string. Safe due to the `int()` cast, but
  use `LIMIT ?` binding.

- [ ] **Status/kind string enums** — job statuses (`queued`, `running`,
  `done`, `failed`, `blocked`), episode statuses, and job kinds are
  magic strings across modules. A `StrEnum` per domain lets `ty` catch
  typos.

- [ ] **Config loading tests** — `config.py` XDG precedence
  (`./config.toml` vs `~/.config/podracer/config.toml`), env var
  overrides, relative-path anchoring are all untested.

- [ ] **Log silently-dropped feed fields** — `feed.py` returns `None` on
  unparseable durations with no logging; unknown formats vanish without
  trace.

- [ ] **Dockerfile: explicit venv path** — `Dockerfile` relies on uv's
  implicit `.venv` discovery for the ENTRYPOINT. Prefer
  `uv venv /venv && uv sync` + an explicit `/venv/bin/python` entrypoint.

- [ ] **Media disk fill policy** — no cleanup/rotation for downloaded
  MP3s; the planned 50 GB LXC disk will fill. Covered by the
  "Disk management / audio pruning" roadmap entry — prioritize it once
  the homelab deploy lands.

## Reviewed and deemed fine (don't re-flag)

- `transcribe.py` `_post_to_whisper_service` reopening the audio file
  per retry attempt — the context manager closes it each attempt; not a
  leak.
- `db/jobs.py` placeholder-building for `DELETE ... IN (...)` — fully
  parameterized; safe.
- Single shared connection is *not* a data-corruption risk (sqlite3 is
  serialized in modern CPython) — the issue is contention/interleaving,
  per the high-priority item above.
- No CSRF / no web-UI auth — accepted for LAN-only single-user
  deployment; revisit with the "Authentication + multi-user" roadmap
  entry.
