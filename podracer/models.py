from functools import cached_property
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from podracer.timestamps import chronological_key, format_timestamp, parse_timestamp


class Podcast(BaseModel):
    id: int
    title: str
    author: str | None = None
    feed_url: str
    artwork_url: str | None = None
    artwork_path: str | None = None
    description: str | None = None
    subscribed: bool = False
    subscribed_at: str | None = None
    last_synced_at: str | None = None
    created_at: str | None = None
    topics: list[str] = []  # genre/topic tags; populated separately, not a column


class Episode(BaseModel):
    id: int
    podcast_id: int
    guid: str
    title: str
    published_at: str | None = None
    audio_url: str
    duration_seconds: int | None = None
    description: str | None = None
    show_notes: str | None = None
    local_path: str | None = None
    file_size_bytes: int | None = None
    status: str = "pending"
    created_at: str | None = None


class FeedItem(BaseModel):
    """A row in the home feed: an episode joined with its show's title and any
    active job, so the feed template renders without per-row follow-up queries."""
    id: int
    podcast_id: int
    title: str
    podcast_title: str
    published_at: str | None = None
    created_at: str | None = None
    recency: str | None = None  # COALESCE(published_at, created_at) — the sort key
    status: str = "pending"
    duration_seconds: int | None = None
    active_kind: str | None = None  # kind of the in-flight job, if any


class EpisodeListItem(BaseModel):
    """A cross-show / per-show episode row for the JSON API.

    Like FeedItem but carries the existence flags (has_summary / has_transcript)
    and, when requested, the raw PodcastSummary JSON (summary_data) — all from a
    single query so the API list endpoint avoids per-row follow-ups. summary_data
    is left NULL unless the caller asked to include summaries; the route parses
    it into structured form."""
    id: int
    podcast_id: int
    podcast_title: str
    title: str
    published_at: str | None = None
    created_at: str | None = None
    status: str = "pending"
    duration_seconds: int | None = None
    active_kind: str | None = None  # kind of the in-flight job, if any
    has_summary: bool = False
    has_transcript: bool = False
    summary_data: str | None = None  # raw PodcastSummary JSON when include_summary


class Transcript(BaseModel):
    id: int
    episode_id: int
    text: str
    model: str
    language: str | None = None
    created_at: str | None = None


class SummaryRecord(BaseModel):
    id: int
    episode_id: int
    data: str
    model: str
    backend: str
    created_at: str | None = None


class FeedMetadata(BaseModel):
    title: str
    author: str | None = None
    description: str | None = None
    artwork_url: str | None = None
    feed_url: str
    categories: list[str] = []  # iTunes categories, e.g. ['Business', 'Investing']


class FeedEpisode(BaseModel):
    guid: str
    title: str
    audio_url: str
    published_at: str | None = None
    duration_seconds: int | None = None
    description: str | None = None
    show_notes: str | None = None


class Job(BaseModel):
    id: int
    episode_id: int
    kind: str
    status: str
    depends_on_job_id: int | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


# --- summary models ----------------------------------------------------------
# The stored PodcastSummary and its parts. These canonicalize timestamps at
# the model boundary so every reader (episode page, JSON API, CLI) sees
# normalized, chronologically sorted data without re-implementing timestamp
# handling.


def _canonicalize_ts(v: str) -> str:
    """Zero-pad a parseable HH:MM:SS stamp; keep a malformed one as-is rather
    than rejecting it — stored summaries that predate validation must still
    deserialize. Malformed stamps surface as ``seconds is None`` and are
    routed to fallbacks (orphan bucket, un-nested render, skipped enrichment);
    rejection of fresh LLM output happens in summarize.py's content checks,
    which have the context (transcript end) the model lacks."""
    secs = parse_timestamp(v)
    return format_timestamp(secs) if secs is not None else v


CanonicalTimestamp = Annotated[str, AfterValidator(_canonicalize_ts)]


class _TimestampedModel(BaseModel):
    """Behavior-only base adding the parse-once ``seconds`` cache. Subclasses
    declare ``timestamp: CanonicalTimestamp = Field(frozen=True)`` themselves,
    at the field position their LLM schema was tuned against (an inherited
    field would hoist itself first in model_json_schema() property order,
    which steers grammar-constrained generation). The field is frozen so the
    cached ``seconds`` can never go stale against a reassigned stamp."""
    if TYPE_CHECKING:
        # Type-only declaration: never executes at runtime, so pydantic sees
        # no field here; subclasses provide the real one.
        timestamp: str

    @cached_property
    def seconds(self) -> int | None:
        return parse_timestamp(self.timestamp)


def _sort_chronologically(items: list) -> None:
    """In-place chronological sort for Chapter/Highlight lists."""
    items.sort(key=lambda x: chronological_key(x.seconds, x.timestamp))


class SpeakerIdentification(BaseModel):
    label: str
    name: str
    role: str
    evidence_timestamp: str
    evidence_quote: str


# Advertiser/sponsor voices the diarizer picks up but that aren't real speakers;
# suppressed from any display of the speaker list (web UI + JSON API).
AD_SPEAKER_KEYWORDS = {
    "advertisement", "ad ", "ad)", "sponsor", "commercial", "promo",
    "voiceover", "disclosure",
}


def is_ad_speaker(s: SpeakerIdentification) -> bool:
    """Whether a speaker entry looks like an ad/sponsor read rather than a guest."""
    role = s.role.lower()
    name = s.name.lower()
    return any(kw in role or kw in name for kw in AD_SPEAKER_KEYWORDS)


class Chapter(_TimestampedModel):
    title: str
    timestamp: CanonicalTimestamp = Field(frozen=True)
    summary: str


class ChapterList(BaseModel):
    chapters: list[Chapter]

    @model_validator(mode="after")
    def _sorted(self) -> "ChapterList":
        _sort_chronologically(self.chapters)
        return self


class Highlight(_TimestampedModel):
    text: str
    timestamp: CanonicalTimestamp = Field(frozen=True)
    speaker: str
    kind: str  # "takeaway" or "opinion"


class HighlightList(BaseModel):
    highlights: list[Highlight]


# Legacy item models — retained so summaries stored before the insights/takes
# consolidation still deserialize. New summaries populate `highlights` instead.
class Insight(BaseModel):
    text: str
    timestamp: CanonicalTimestamp
    speaker: str


class SpeakerTake(BaseModel):
    speaker: str
    take: str
    timestamp: CanonicalTimestamp


class PodcastSummary(BaseModel):
    # Stored blobs round-trip through this model (CLI --json, migrations), so
    # keep fields a newer version may have written instead of silently
    # dropping them on re-serialization.
    model_config = ConfigDict(extra="allow")

    summary: str
    speakers: list[SpeakerIdentification]
    chapters: list[Chapter]
    highlights: list[Highlight] = []
    # Legacy fields, retained for reading pre-consolidation summaries.
    insights: list[Insight] = []
    speaker_takes: list[SpeakerTake] = []

    @model_validator(mode="after")
    def _sorted(self) -> "PodcastSummary":
        # Canonical order at the model boundary: every consumer (episode page,
        # JSON API, digest) reads chapters/highlights chronologically sorted
        # without re-implementing timestamp handling.
        _sort_chronologically(self.chapters)
        _sort_chronologically(self.highlights)
        return self

    def effective_highlights(self) -> list[Highlight]:
        """Highlights to display, migrating legacy insights/takes on read.
        Always chronologically sorted (unparseable timestamps last)."""
        if self.highlights:
            return self.highlights
        merged = [
            Highlight(text=i.text, timestamp=i.timestamp, speaker=i.speaker, kind="takeaway")
            for i in self.insights
        ]
        merged += [
            Highlight(text=t.take, timestamp=t.timestamp, speaker=t.speaker, kind="opinion")
            for t in self.speaker_takes
        ]
        _sort_chronologically(merged)
        return merged

    def migrated(self) -> "PodcastSummary":
        """The canonical read view served to agents: legacy insights/takes
        folded into highlights and ad/sponsor voices dropped — the same shape
        the JSON API returns."""
        return PodcastSummary(
            summary=self.summary,
            speakers=[s for s in self.speakers if not is_ad_speaker(s)],
            chapters=self.chapters,
            highlights=self.effective_highlights(),
        )
