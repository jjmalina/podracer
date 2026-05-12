from pydantic import BaseModel


class Podcast(BaseModel):
    id: int
    title: str
    author: str | None = None
    feed_url: str
    artwork_url: str | None = None
    description: str | None = None
    subscribed: bool = False
    last_synced_at: str | None = None
    created_at: str | None = None


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


class FeedEpisode(BaseModel):
    guid: str
    title: str
    audio_url: str
    published_at: str | None = None
    duration_seconds: int | None = None
    description: str | None = None
    show_notes: str | None = None
