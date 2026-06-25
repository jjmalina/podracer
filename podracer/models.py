from pydantic import BaseModel


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


class DigestRecord(BaseModel):
    """A stored digest row. `data` is raw DigestData JSON, parsed by callers —
    mirroring SummaryRecord, so the DB layer never depends on podracer.digest."""
    id: int
    kind: str
    period_start: str
    period_end: str
    data: str
    episode_count: int
    model: str
    backend: str
    created_at: str | None = None


class DigestMemberRow(BaseModel):
    """One summarized episode that falls in a digest's period window, joined with
    its show title and the raw PodcastSummary JSON. digest.generate_and_save
    enriches these with topics and turns each into LLM input."""
    episode_id: int
    podcast_id: int
    podcast_title: str
    title: str
    recency: str | None = None  # COALESCE(published_at, created_at)
    summary_data: str


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
