"""Data models for suno-dl."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Context-specific bounds for third-party text (#157). Titles, lyrics and tags
# come from public pages and can be arbitrarily long: rather than rejecting a
# whole track over one hostile field, the value is truncated to a generous
# bound that no legitimate entry reaches, so filenames, tags and sidecars stay
# bounded while the track itself is never silently dropped.
MAX_TITLE_CHARS = 1000
MAX_LYRICS_CHARS = 100_000
MAX_TAGS_CHARS = 4096


class TrackInfo(BaseModel):
    """A single discovered track."""

    id: str
    title: str
    artist: str | None = None
    creator_handle: str | None = None  # clip-local handle, separate from source provenance
    artist_is_fallback: bool = Field(default=False, exclude=True)
    artist_is_ambiguous: bool = Field(default=False, exclude=True)
    from_playlist: bool = Field(default=False, exclude=True)  # playlist owner is not a producer
    duration_sec: int | None = None
    page_url: str
    audio_url: str | None = None
    cover_url: str | None = None
    media_urls: list[str] = []
    created_at: str | None = Field(
        default=None,
        description="Suno creation timestamp (ISO 8601) when the payload carried it; "
        "the rotation's primary age key, with file mtime as the fallback (#93).",
    )
    lyrics: str | None = None
    style_tags: list[str] = []
    play_count: int | None = None
    model_version: str | None = None
    # Public metadata recorded in the per-track sidecar (see suno_dl.sidecar).
    model_name: str | None = None
    tags: str | None = None
    is_public: bool | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _bound_title(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > MAX_TITLE_CHARS:
            return value[:MAX_TITLE_CHARS]
        return value

    @field_validator("lyrics", mode="before")
    @classmethod
    def _bound_lyrics(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > MAX_LYRICS_CHARS:
            return value[:MAX_LYRICS_CHARS]
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _bound_tags(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > MAX_TAGS_CHARS:
            return value[:MAX_TAGS_CHARS]
        return value

    @field_validator("duration_sec", "play_count", mode="before")
    @classmethod
    def _bound_count(cls, value: object) -> object:
        """Drop counters that cannot be true (negatives, absurd magnitudes).

        Strings and floats are handed back so pydantic keeps coercing them as
        before; only a clearly bogus integer becomes ``None``.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return value
        if value < 0 or value > 10**9:
            return None
        return value
