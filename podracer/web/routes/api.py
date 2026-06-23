"""Read-only JSON API under /api/v1.

Lets programs — an aggregator that digests a topic, a script, an agent — read
podcasts, the cross-show feed, episodes, summaries, and transcripts over HTTP
instead of scraping the HTML UI. GET-only in this slice: writes (subscribe,
enqueue, ingest) and auth land later (see docs/plans/2026-06-22-rest-api.md).

Responses route through purpose-built models so the JSON shape is the contract,
not whatever a SQLite row happens to look like.
"""
import sqlite3
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from podracer.db import (
    count_episodes,
    count_podcasts,
    get_active_kind,
    get_all_tags,
    get_episode,
    get_podcast,
    get_podcasts,
    get_summary,
    get_transcript,
    list_episodes,
    summary_exists,
    transcript_exists,
)
from podracer.models import EpisodeListItem, Transcript
from podracer.summarize import (
    Chapter,
    Highlight,
    PodcastSummary,
    SpeakerIdentification,
    is_ad_speaker,
)
from podracer.web.deps import get_db

API_PREFIX = "/api/v1"
router = APIRouter(prefix=API_PREFIX, tags=["api"])

# Bumped on a breaking change to the response shape — independent of the DB
# migration state. Clients pin against this.
SCHEMA_VERSION = "v1"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200           # hard cap for metadata-only listings
SUMMARY_MAX_LIMIT = 50    # lower cap when ?include=summary embeds full summaries

# The episode lifecycle, plus 'all' (= no filter). Typed as a Literal so OpenAPI
# emits the enum (Swagger renders a dropdown) and FastAPI validates the query
# value itself — a bad status is a 422 with no manual check.
StatusFilter = Literal["pending", "downloaded", "transcribed", "summarized", "all"]


# --- response models ---------------------------------------------------------


class ApiSummary(BaseModel):
    """A normalized summary: legacy insights/speaker_takes are migrated into
    highlights on read (via PodcastSummary.effective_highlights), so the API
    never exposes the pre-consolidation shape."""
    summary: str
    speakers: list[SpeakerIdentification]
    chapters: list[Chapter]
    highlights: list[Highlight]


class ApiEpisode(BaseModel):
    """A row in a list response. summary is present only with ?include=summary."""
    id: int
    podcast_id: int
    podcast_title: str
    title: str
    published_at: str | None = None
    status: str
    duration_seconds: int | None = None
    has_summary: bool
    has_transcript: bool
    active_job: str | None = None  # kind of the in-flight job, if any
    summary: ApiSummary | None = None


class ApiEpisodeDetail(BaseModel):
    """Full episode metadata plus the show's topics and artifact flags.

    Explicit field list (not a subclass of the storage model Episode) so the
    server-internal local_path never reaches the wire and a new episodes column
    can't silently leak into the public contract."""
    id: int
    podcast_id: int
    podcast_title: str | None = None
    guid: str
    title: str
    published_at: str | None = None
    audio_url: str
    duration_seconds: int | None = None
    description: str | None = None
    show_notes: str | None = None
    file_size_bytes: int | None = None
    status: str
    created_at: str | None = None
    topics: list[str] = []
    has_summary: bool = False
    has_transcript: bool = False
    active_job: str | None = None


class ApiPodcast(BaseModel):
    """Public podcast shape — drops artwork_path (a server filesystem path);
    artwork is served via the cover endpoint, not exposed as a local path."""
    id: int
    title: str
    author: str | None = None
    feed_url: str
    artwork_url: str | None = None
    description: str | None = None
    subscribed: bool = False
    subscribed_at: str | None = None
    last_synced_at: str | None = None
    created_at: str | None = None
    topics: list[str] = []


class EpisodePage(BaseModel):
    items: list[ApiEpisode]
    total: int
    limit: int
    offset: int


class PodcastPage(BaseModel):
    items: list[ApiPodcast]
    total: int
    limit: int
    offset: int


class TagList(BaseModel):
    tags: list[str]


class Version(BaseModel):
    podracer_version: str
    schema_version: str


# --- helpers -----------------------------------------------------------------


def _wants_summary(include: str | None) -> bool:
    """Whether ?include asked for embedded summaries; 422 on any other value so a
    typo ('summaries', 'Summary') fails loudly instead of silently omitting them."""
    if include is None:
        return False
    if include != "summary":
        raise HTTPException(
            status_code=422, detail=f"invalid include '{include}'; expected 'summary'",
        )
    return True


def _clamp(limit: int, offset: int, include_summary: bool) -> tuple[int, int]:
    cap = SUMMARY_MAX_LIMIT if include_summary else MAX_LIMIT
    return max(1, min(limit, cap)), max(0, offset)


def _parse_summary(data: str) -> ApiSummary:
    s = PodcastSummary.model_validate_json(data)
    return ApiSummary(
        summary=s.summary,
        # Drop ad/sponsor voices, matching the web episode page (web/routes/episodes.py).
        speakers=[sp for sp in s.speakers if not is_ad_speaker(sp)],
        chapters=s.chapters,
        highlights=s.effective_highlights(),
    )


def _to_api_episode(row: EpisodeListItem, include_summary: bool) -> ApiEpisode:
    summary = None
    if include_summary and row.summary_data:
        try:
            summary = _parse_summary(row.summary_data)
        except Exception:
            summary = None  # a corrupt blob shouldn't sink the whole list
    return ApiEpisode(
        id=row.id,
        podcast_id=row.podcast_id,
        podcast_title=row.podcast_title,
        title=row.title,
        published_at=row.published_at,
        status=row.status,
        duration_seconds=row.duration_seconds,
        has_summary=row.has_summary,
        has_transcript=row.has_transcript,
        active_job=row.active_kind,
        summary=summary,
    )


# --- podcasts ----------------------------------------------------------------


@router.get("/podcasts")
def api_list_podcasts(
    db: sqlite3.Connection = Depends(get_db),
    tag: list[str] | None = Query(None),  # repeatable: ?tag=Finance&tag=Macro (OR)
    subscribed: bool = True,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> PodcastPage:
    limit, offset = _clamp(limit, offset, include_summary=False)
    total = count_podcasts(db, tags=tag, subscribed_only=subscribed)
    rows = get_podcasts(db, tags=tag, subscribed_only=subscribed, limit=limit, offset=offset)
    items = [ApiPodcast(**p.model_dump()) for p in rows]
    return PodcastPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/podcasts/{podcast_id}")
def api_get_podcast(
    podcast_id: int, db: sqlite3.Connection = Depends(get_db),
) -> ApiPodcast:
    podcast = get_podcast(db, podcast_id)
    if podcast is None:
        raise HTTPException(status_code=404, detail="podcast not found")
    return ApiPodcast(**podcast.model_dump())


@router.get("/podcasts/{podcast_id}/episodes")
def api_podcast_episodes(
    podcast_id: int,
    db: sqlite3.Connection = Depends(get_db),
    status: StatusFilter | None = None,
    include: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> EpisodePage:
    if get_podcast(db, podcast_id) is None:
        raise HTTPException(status_code=404, detail="podcast not found")
    include_summary = _wants_summary(include)
    limit, offset = _clamp(limit, offset, include_summary)
    total = count_episodes(db, podcast_id=podcast_id, status=status, subscribed_only=False)
    rows = list_episodes(
        db, podcast_id=podcast_id, status=status, subscribed_only=False,
        limit=limit, offset=offset, include_summary=include_summary,
    )
    return EpisodePage(
        items=[_to_api_episode(r, include_summary) for r in rows],
        total=total, limit=limit, offset=offset,
    )


# --- episodes (cross-show feed) ----------------------------------------------


@router.get("/episodes")
def api_list_episodes(
    db: sqlite3.Connection = Depends(get_db),
    tag: list[str] | None = Query(None),  # repeatable: ?tag=Finance&tag=Macro (OR)
    status: StatusFilter | None = None,
    subscribed: bool = True,
    include: str | None = None,           # "summary" to embed full summaries
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> EpisodePage:
    include_summary = _wants_summary(include)
    limit, offset = _clamp(limit, offset, include_summary)
    total = count_episodes(db, tags=tag, status=status, subscribed_only=subscribed)
    rows = list_episodes(
        db, tags=tag, status=status, subscribed_only=subscribed,
        limit=limit, offset=offset, include_summary=include_summary,
    )
    return EpisodePage(
        items=[_to_api_episode(r, include_summary) for r in rows],
        total=total, limit=limit, offset=offset,
    )


@router.get("/episodes/{episode_id}")
def api_get_episode(
    episode_id: int, db: sqlite3.Connection = Depends(get_db),
) -> ApiEpisodeDetail:
    episode = get_episode(db, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    podcast = get_podcast(db, episode.podcast_id)
    return ApiEpisodeDetail(
        **episode.model_dump(),
        podcast_title=podcast.title if podcast else None,
        topics=podcast.topics if podcast else [],
        # Existence probes, not the full blobs — get_summary/get_transcript would
        # load the entire summary JSON and ~200 KB transcript just for a bool.
        has_summary=summary_exists(db, episode_id),
        has_transcript=transcript_exists(db, episode_id),
        active_job=get_active_kind(db, episode_id),
    )


@router.get("/episodes/{episode_id}/summary")
def api_episode_summary(
    episode_id: int, db: sqlite3.Connection = Depends(get_db),
) -> ApiSummary:
    if get_episode(db, episode_id) is None:
        raise HTTPException(status_code=404, detail="episode not found")
    record = get_summary(db, episode_id)
    if record is None:
        raise HTTPException(status_code=404, detail="summary not found")
    try:
        return _parse_summary(record.data)
    except Exception:
        raise HTTPException(status_code=500, detail="stored summary could not be parsed") from None


@router.get("/episodes/{episode_id}/transcript")
def api_episode_transcript(
    episode_id: int, db: sqlite3.Connection = Depends(get_db),
) -> Transcript:
    if get_episode(db, episode_id) is None:
        raise HTTPException(status_code=404, detail="episode not found")
    transcript = get_transcript(db, episode_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="transcript not found")
    return transcript


# --- discovery ---------------------------------------------------------------


@router.get("/tags")
def api_tags(
    db: sqlite3.Connection = Depends(get_db), subscribed: bool = True,
) -> TagList:
    return TagList(tags=get_all_tags(db, subscribed_only=subscribed))


@router.get("/version")
def api_version() -> Version:
    try:
        ver = pkg_version("podracer")
    except PackageNotFoundError:
        ver = "unknown"
    return Version(podracer_version=ver, schema_version=SCHEMA_VERSION)
