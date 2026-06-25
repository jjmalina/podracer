"""Daily & weekly digests: a stored summary-of-summaries per local period.

A digest reads the period's *stored episode summaries* (never transcripts), asks
the LLM for one tight sentence per episode plus a short overview, and assembles a
deterministic topic -> show -> episode tree from the shows' tags. Generation
reuses the summarize machinery (Backend, _checked_or_fail, the degenerate-output
retry loop); only the language is the model's, the structure is Python's.

All timezone/period math lives here; the db.digests layer stays SQL-only.
"""
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel

from podracer import logger
from podracer.config import Config
from podracer.db import (
    count_digest_members,
    get_config,
    get_digest,
    get_digest_members,
    save_digest,
    set_config,
    topics_by_podcast,
)
from podracer.models import DigestMemberRow
from podracer.summarize import (
    Backend,
    DegenerateOutputError,
    PodcastSummary,
    _checked_or_fail,
    _ends_terminally,
)

OTHER_TOPIC = "Other"  # bucket for shows with no topic tags; always sorts last

DIGEST_WATERMARK_KEY = "digest_watermark"

# How far back the scheduler rechecks each tick. A finalized period older than
# this won't be auto-(re)generated — backfill is a deliberate CLI op — which is
# what keeps a tick from recounting all history.
_DAILY_HORIZON_DAYS = 14
_WEEKLY_HORIZON_WEEKS = 6

_MAX_HIGHLIGHTS_PER_EPISODE = 5  # how many of an episode's highlights to feed the model
_DISPLAY_HIGHLIGHTS = 4          # cap on highlights shown per episode in the digest
_MIN_OVERVIEW_CHARS = 40         # a shorter overview is a stub
_MIN_BLURB_CHARS = 80            # a 2-3 sentence blurb shorter than this is a stub
_MIN_HIGHLIGHT_CHARS = 15        # a shorter highlight is a stub

DIGEST_PROMPT = """\
You are compiling a {period} digest: a roundup of the podcast episodes from a \
single {noun}. You will be given a list of episodes — each with a numeric \
episode_id, its show, its title, a short summary, and a few highlights.

For EACH episode, write:
- a `blurb`: 2-3 sentences capturing what the episode was actually about and \
why it's worth knowing. Be specific and concrete — names, numbers, the real \
claim — not "they discussed X". Vary the openings; don't start every blurb with \
the show or a host's name.
- `highlights`: 2-4 short bullet points, each a single tight, standalone \
sentence stating one concrete takeaway from that episode.

Then write a 1-2 sentence `overview` of the {noun} across all the shows: the \
throughline, or the handful of things that stand out. Keep it crisp.

Rules:
- Return exactly one item per episode, keyed by its episode_id. Do not invent \
episodes or episode_ids, and never drop one.
- Every blurb and every highlight is a complete sentence ending in punctuation.
- Synthesize only from the provided summaries and highlights; do not add facts \
that aren't there."""


# --- stored / LLM shapes -----------------------------------------------------


class DigestItem(BaseModel):
    """One per-episode entry, as the model returns it."""
    episode_id: int
    blurb: str
    highlights: list[str]


class DigestLLMOutput(BaseModel):
    """The model's structured response: an overview plus a flat per-episode list.
    Python builds the topic tree; the model only writes language."""
    overview: str
    items: list[DigestItem]


class DigestEpisode(BaseModel):
    episode_id: int
    title: str          # snapshot at generation time; episode_id keeps links live
    blurb: str          # 2-3 sentence synthesis
    highlights: list[str] = []


class DigestShow(BaseModel):
    podcast_id: int
    podcast_title: str
    episodes: list[DigestEpisode]


class DigestTopic(BaseModel):
    topic: str                  # e.g. 'Technology'; 'Other' when untagged
    shows: list[DigestShow]
    episode_count: int          # episodes under this topic


class DigestData(BaseModel):
    overview: str
    topics: list[DigestTopic]   # topic -> show -> episode
    episode_count: int          # DISTINCT episodes across the period


@dataclass
class DigestMember:
    """A summarized episode prepared as LLM input for a digest run."""
    episode_id: int
    podcast_id: int
    podcast_title: str
    title: str
    topics: list[str]
    summary_prose: str
    top_highlights: list[str] = field(default_factory=list)


# --- periods + timing --------------------------------------------------------


@dataclass(frozen=True)
class Period:
    kind: str        # 'day' | 'week'
    start: date      # local date; a week's Monday
    end: date        # exclusive local-date bound

    @property
    def start_str(self) -> str:
        return self.start.isoformat()

    @property
    def end_str(self) -> str:
        return self.end.isoformat()


def day_period(d: date) -> Period:
    return Period("day", d, d + timedelta(days=1))


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_period(d: date) -> Period:
    """The ISO week (Mon–Sun) containing `d`, keyed by its Monday."""
    start = _monday_of(d)
    return Period("week", start, start + timedelta(days=7))


def local_today(cfg: Config) -> date:
    """Today's date in the configured digest timezone."""
    return datetime.now(ZoneInfo(cfg.digest_timezone)).date()


def utc_bounds(start: date, end: date, tz: str) -> tuple[str, str]:
    """UTC instants for the local [start, end) window, formatted to match the
    stored timestamps. zoneinfo makes this DST-correct."""
    z = ZoneInfo(tz)
    lo = datetime.combine(start, time.min, z).astimezone(UTC)
    hi = datetime.combine(end, time.min, z).astimezone(UTC)
    fmt = "%Y-%m-%d %H:%M:%S"  # matches datetime('now')
    return lo.strftime(fmt), hi.strftime(fmt)


def is_finalizable(period: Period, now_local: datetime, hour: int) -> bool:
    """A period is finalizable once the local clock passes `hour` on its exclusive
    end date — H:00 on D+1 for a day, on the following Monday for a week."""
    finalize_at = datetime.combine(period.end, time(hour=hour), now_local.tzinfo)
    return now_local >= finalize_at


def format_period_label(kind: str, start: date, end: date) -> str:
    """Human label for a period: 'Tue · Jun 23' / 'Week of Jun 16–22'."""
    if kind == "week":
        last = end - timedelta(days=1)
        if start.month == last.month:
            return f"Week of {start:%b} {start.day}–{last.day}"
        return f"Week of {start:%b} {start.day} – {last:%b} {last.day}"
    return f"{start:%a} · {start:%b} {start.day}"


# --- watermark + scheduling --------------------------------------------------


def init_digest_watermark(conn: sqlite3.Connection, cfg: Config) -> date:
    """Record today's local date as the backfill floor on first run; return the
    stored watermark. Mirrors init_worker_watermark / subscribed_at: auto
    generation never reaches back before this."""
    existing = get_config(conn, DIGEST_WATERMARK_KEY)
    if existing:
        return date.fromisoformat(existing)
    today = datetime.now(ZoneInfo(cfg.digest_timezone)).date()
    set_config(conn, DIGEST_WATERMARK_KEY, today.isoformat())
    return today


def _needs_generation(conn: sqlite3.Connection, cfg: Config, period: Period) -> bool:
    """Whether a finalizable period has no row yet (and has members) or has gone
    stale — its live membership count exceeds the stored one (a straggler folded
    in after the period closed)."""
    lo, hi = utc_bounds(period.start, period.end, cfg.digest_timezone)
    live = count_digest_members(conn, lo, hi)
    existing = get_digest(conn, period.kind, period.start_str)
    if existing is None:
        return live > 0  # empty period -> never write a row
    return live > existing.episode_count


def due_periods(conn: sqlite3.Connection, cfg: Config) -> list[Period]:
    """Finalizable periods, on/after the watermark, that are missing or stale.

    Bounded to a recent horizon per kind so a tick costs a handful of cheap count
    queries, not a full-history scan. Dailies first, then weeklies."""
    tz = ZoneInfo(cfg.digest_timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    watermark = init_digest_watermark(conn, cfg)
    hour = cfg.digest_hour
    out: list[Period] = []

    for i in range(1, _DAILY_HORIZON_DAYS + 1):
        p = day_period(today - timedelta(days=i))
        if p.end <= watermark or not is_finalizable(p, now_local, hour):
            continue
        if _needs_generation(conn, cfg, p):
            out.append(p)

    this_monday = _monday_of(today)
    for i in range(1, _WEEKLY_HORIZON_WEEKS + 1):
        p = week_period(this_monday - timedelta(days=7 * i))
        if p.end <= watermark or not is_finalizable(p, now_local, hour):
            continue
        if _needs_generation(conn, cfg, p):
            out.append(p)

    return out


# --- generation --------------------------------------------------------------


def _build_digest_backend(cfg: Config, backend: str | None, model: str | None) -> Backend:
    """Resolve the digest LLM backend. Defaults to the summarize backend/model
    unless a digest-specific override (config or arg) is set. Mirrors
    process._build_summarize_backend."""
    backend_name = backend or cfg.digest_backend or cfg.summarize_backend
    model_name = model or cfg.digest_model or cfg.summarize_model
    base_url = cfg.summarize_base_url
    if backend_name == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY") or cfg.openrouter_api_key
        if not api_key:
            raise RuntimeError("openrouter backend requires OPENROUTER_API_KEY")
        return Backend.openrouter(model_name, api_key)
    if backend_name == "vllm":
        return Backend.vllm(model_name, base_url or "http://localhost:8000")
    return Backend.ollama(model_name, base_url or "http://localhost:11434")


def _digest_prompt(kind: str) -> str:
    if kind == "week":
        return DIGEST_PROMPT.format(period="weekly", noun="week")
    return DIGEST_PROMPT.format(period="daily", noun="day")


def _build_user_message(members: list[DigestMember], kind: str) -> str:
    noun = "week" if kind == "week" else "day"
    out = [f"Episodes from this {noun} ({len(members)} total):", ""]
    for m in members:
        out.append(f"episode_id: {m.episode_id}")
        out.append(f"show: {m.podcast_title}")
        out.append(f"title: {m.title}")
        out.append(f"summary: {m.summary_prose.strip()}")
        if m.top_highlights:
            out.append("highlights:")
            out.extend(f"  - {h.strip()}" for h in m.top_highlights)
        out.append("")
    return "\n".join(out)


def _make_digest_check(members: list[DigestMember]):
    """A content check for _checked_or_fail: a real overview, a usable 2-3
    sentence blurb for every member episode (no dropped, no stub), and highlights
    that weren't dropped wholesale (at least one per episode on average).
    Hallucinated episode_ids are ignored here and again when assembling."""
    member_ids = {m.episode_id for m in members}

    def check(out: DigestLLMOutput) -> None:
        overview = out.overview.strip()
        if len(overview) < _MIN_OVERVIEW_CHARS or not _ends_terminally(overview):
            raise DegenerateOutputError(
                "digest", f"overview stub ({len(overview)} chars)", output_chars=len(overview))
        with_blurb: set[int] = set()
        usable_highlights = 0
        for it in out.items:
            if it.episode_id not in member_ids:
                continue
            blurb = it.blurb.strip()
            if len(blurb) >= _MIN_BLURB_CHARS and _ends_terminally(blurb):
                with_blurb.add(it.episode_id)
            usable_highlights += sum(
                1 for h in it.highlights
                if len(h.strip()) >= _MIN_HIGHLIGHT_CHARS and _ends_terminally(h.strip()))
        missing = member_ids - with_blurb
        if missing:
            raise DegenerateOutputError(
                "digest", f"{len(missing)} of {len(member_ids)} episodes missing/stub blurbs")
        if usable_highlights < len(member_ids):
            raise DegenerateOutputError(
                "digest", f"too few highlights ({usable_highlights} for {len(member_ids)} episodes)")

    return check


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _fallback_blurb(member: DigestMember) -> str:
    """First two sentences of the stored summary, used only when the model
    dropped an episode after retries — so the digest still renders it."""
    text = " ".join(member.summary_prose.split())
    blurb = " ".join(_SENT_SPLIT.split(text)[:2]).strip()
    return blurb or member.title


def _assemble(
    members: list[DigestMember], overview: str,
    items: dict[int, tuple[str, list[str] | None]],
) -> DigestData:
    """Build the topic -> show -> episode tree. Each show is placed under a single
    *primary* topic (its first tag) so an episode never repeats across sections.
    members are already recency-desc, so episode order within a show is too."""
    topics: dict[str, dict[int, DigestShow]] = {}
    for m in members:
        blurb, highlights = items.get(m.episode_id, (None, None))
        ep = DigestEpisode(
            episode_id=m.episode_id, title=m.title,
            blurb=blurb or _fallback_blurb(m),
            highlights=highlights if highlights else m.top_highlights[:_DISPLAY_HIGHLIGHTS],
        )
        primary = m.topics[0] if m.topics else OTHER_TOPIC
        shows = topics.setdefault(primary, {})
        show = shows.get(m.podcast_id)
        if show is None:
            show = DigestShow(podcast_id=m.podcast_id, podcast_title=m.podcast_title, episodes=[])
            shows[m.podcast_id] = show
        show.episodes.append(ep)

    topic_objs: list[DigestTopic] = []
    for name, shows in topics.items():
        show_list = sorted(shows.values(), key=lambda s: (-len(s.episodes), s.podcast_title.lower()))
        topic_objs.append(DigestTopic(
            topic=name, shows=show_list,
            episode_count=sum(len(s.episodes) for s in show_list),
        ))
    # Most-covered topic first; 'Other' always last.
    topic_objs.sort(key=lambda t: (t.topic == OTHER_TOPIC, -t.episode_count, t.topic.lower()))
    return DigestData(overview=overview, topics=topic_objs, episode_count=len(members))


def generate_digest(members: list[DigestMember], *, backend: Backend, kind: str) -> DigestData:
    """LLM pass + deterministic assembly. Raises (failing the caller) only when
    nothing ever parsed; a degraded-but-parsed response is accepted and any
    dropped episode falls back to its summary + stored highlights."""
    out = _checked_or_fail(
        DigestLLMOutput, backend, _digest_prompt(kind),
        _build_user_message(members, kind), _make_digest_check(members),
    )
    items: dict[int, tuple[str, list[str] | None]] = {}
    for it in out.items:
        blurb = it.blurb.strip()
        if not blurb:
            continue
        highlights = [h.strip() for h in it.highlights if h.strip()][:_DISPLAY_HIGHLIGHTS]
        items[it.episode_id] = (blurb, highlights or None)
    return _assemble(members, out.overview.strip(), items)


def _member_input(
    row: DigestMemberRow, daily: dict[int, tuple[str, list[str]]],
) -> tuple[str, list[str]]:
    """The LLM input (prose + highlights) for one member. For a weekly, an episode
    already covered by a daily digest is fed that daily blurb + highlights
    (hierarchical, ~10x smaller); otherwise its stored summary + a few highlights."""
    if row.episode_id in daily:
        return daily[row.episode_id]
    try:
        summ = PodcastSummary.model_validate_json(row.summary_data)
    except Exception:
        return row.title, []  # corrupt blob shouldn't sink the digest
    highs = [h.text for h in summ.effective_highlights()[:_MAX_HIGHLIGHTS_PER_EPISODE]]
    return summ.summary, highs


def _week_daily_episodes(
    conn: sqlite3.Connection, period: Period,
) -> dict[int, tuple[str, list[str]]]:
    """episode_id -> (blurb, highlights) from this week's stored daily digests,
    for the hierarchical weekly pass. Empty for days without a daily digest."""
    out: dict[int, tuple[str, list[str]]] = {}
    d = period.start
    while d < period.end:
        rec = get_digest(conn, "day", d.isoformat())
        if rec:
            try:
                data = DigestData.model_validate_json(rec.data)
            except Exception:
                data = None
            if data:
                for topic in data.topics:
                    for show in topic.shows:
                        for ep in show.episodes:
                            out.setdefault(ep.episode_id, (ep.blurb, ep.highlights))
        d += timedelta(days=1)
    return out


def generate_and_save(
    conn: sqlite3.Connection, cfg: Config, period: Period, *,
    backend: str | None = None, model: str | None = None,
) -> DigestData | None:
    """Build a period's digest from its members and upsert it. Returns the
    DigestData, or None for an empty period (no row written)."""
    lo, hi = utc_bounds(period.start, period.end, cfg.digest_timezone)
    rows = get_digest_members(conn, lo, hi)
    if not rows:
        logger.info("digest_empty", kind=period.kind, start=period.start_str)
        return None

    topics_map = topics_by_podcast(conn, list({r.podcast_id for r in rows}))
    daily = _week_daily_episodes(conn, period) if period.kind == "week" else {}
    members = []
    for r in rows:
        prose, highs = _member_input(r, daily)
        members.append(DigestMember(
            episode_id=r.episode_id, podcast_id=r.podcast_id,
            podcast_title=r.podcast_title, title=r.title,
            topics=topics_map.get(r.podcast_id, []),
            summary_prose=prose, top_highlights=highs,
        ))

    llm = _build_digest_backend(cfg, backend, model)
    with structlog.contextvars.bound_contextvars(digest_kind=period.kind, digest_start=period.start_str):
        data = generate_digest(members, backend=llm, kind=period.kind)
    save_digest(conn, period.kind, period.start_str, period.end_str,
                data.model_dump_json(), data.episode_count, llm.model, llm.name)
    logger.info("digest_saved", kind=period.kind, start=period.start_str,
                episodes=data.episode_count, topics=len(data.topics))
    return data
