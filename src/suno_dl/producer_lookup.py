"""Clip-local producer verification from Suno's document head metadata.

Lyrics and React Flight text are never scanned for identity. The requested UUID,
canonical URL, Open Graph song title, document title and publisher description
must agree. Unsupported or ambiguous pages fail closed.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .discovery import _validated_identity
from .fetcher import Fetcher
from .models import TrackInfo
from .urls import UUID_RE, song_page_url


class ProducerLookupError(RuntimeError):
    """The exact song page did not establish one producer."""


@dataclass(frozen=True)
class ProducerIdentity:
    artist: str
    handle: str
    title: str


def _require_trusted_framing() -> None:
    """Compatibility capability check: own-head verification replaces Flight."""
    return None


def refuse_filename_attribution() -> None:
    """An existing filename does not establish its producer."""
    raise ProducerLookupError(
        "Producer lookup unavailable: inferred filename attribution has no verified "
        "framing and clip association; use the reviewed producer migration"
    )


def producer_identity_from_page(clip_id: str, html: str) -> ProducerIdentity:
    if (
        not isinstance(clip_id, str)
        or not UUID_RE.fullmatch(clip_id)
        or not isinstance(html, str)
        or len(html) > 8 * 1024 * 1024
    ):
        raise ProducerLookupError("invalid UUID or oversized song page")
    try:
        encoded_size = len(html.encode("utf-8"))
    except UnicodeError as exc:
        raise ProducerLookupError("invalid page text encoding") from exc
    if encoded_size > 8 * 1024 * 1024:
        raise ProducerLookupError("invalid UUID or oversized song page")
    soup = BeautifulSoup(html, "html.parser")
    heads = soup.find_all("head")
    if len(heads) != 1:
        raise ProducerLookupError("song page lacks a unique head")
    head = heads[0]

    def only(selector: str, attribute: str | None = None) -> str:
        tags = head.select(selector)
        if len(tags) != 1:
            raise ProducerLookupError("missing or ambiguous song metadata: " + selector)
        value = tags[0].get(attribute) if attribute else tags[0].get_text()
        if not isinstance(value, str) or not value:
            raise ProducerLookupError("empty song metadata")
        return value

    canonical = only('link[rel~="canonical"]', "href")
    if canonical != song_page_url(clip_id.lower()):
        raise ProducerLookupError("song canonical does not match requested UUID")
    title = only('meta[property="og:title"]', "content")
    if len(title) > 1000 or len(title.encode("utf-8")) > 4096:
        raise ProducerLookupError("song title exceeds the metadata bound")
    description = only('meta[name="description"]', "content")
    prefix = title + " by "
    suffix = "). Listen and make your own on Suno."
    if not description.startswith(prefix) or not description.endswith(suffix):
        raise ProducerLookupError("song description does not match its title")
    identity = description[len(prefix) : -len(suffix)]
    match = re.fullmatch(r"(.+) \(@([A-Za-z0-9_-]{1,255})", identity)
    if match is None:
        raise ProducerLookupError("song description lacks a producer handle")
    display, handle = match.groups()
    if only("title") != title + " by " + display + " | Suno":
        raise ProducerLookupError("song producer metadata disagrees")
    artist = _validated_identity({"display_name": display}, ("display_name",))
    if artist is None:
        artist = _validated_identity({"handle": handle}, ("handle",))
    if artist is None:
        raise ProducerLookupError("invalid song producer")
    return ProducerIdentity(artist, handle, title)


async def resolve_producers(
    clip_ids: Iterable[str], fetcher: Fetcher, *, delay_sec: float = 1.5
) -> AsyncIterator[tuple[str, str | None]]:
    raw_ids = list(clip_ids)
    if any(not isinstance(clip_id, str) or not UUID_RE.fullmatch(clip_id) for clip_id in raw_ids):
        raise ProducerLookupError("invalid clip ID")
    ids = [clip_id.lower() for clip_id in raw_ids]
    if len(ids) != len(set(ids)):
        raise ProducerLookupError("repeated clip ID")
    if (
        isinstance(delay_sec, bool)
        or not isinstance(delay_sec, (int, float))
        or not math.isfinite(delay_sec)
        or delay_sec < 0
    ):
        raise ProducerLookupError("invalid delay")
    for position, clip_id in enumerate(ids):
        if position and delay_sec:
            await asyncio.sleep(delay_sec)
        try:
            html = await fetcher.fetch_song_page(clip_id)
            identity = producer_identity_from_page(clip_id, html)
        except ProducerLookupError:
            raise
        except Exception as exc:
            raise ProducerLookupError(f"could not inspect clip {clip_id}") from exc
        yield clip_id, identity.artist


async def verify_track_producers(tracks: list[TrackInfo], fetcher: Fetcher) -> list[TrackInfo]:
    """Return isolated tracks with mandatory own-page identity and media URLs.

    Caller-supplied artist/title/handle and media URLs are never evidence.
    Validate the entire ID list before HTTP, fetch each UUID once, and do not
    mutate the caller's objects if one page cannot establish identity.
    """
    from .verified_sync import media_urls

    if any(not isinstance(t.id, str) or not UUID_RE.fullmatch(t.id) for t in tracks):
        raise ProducerLookupError("invalid clip ID")
    if any(track.is_public is False for track in tracks):
        raise ProducerLookupError("explicitly non-public clips cannot be selected")
    decisions = {}
    for track in tracks:
        clip_id = track.id.lower()
        if clip_id in decisions:
            continue
        try:
            html = await fetcher.fetch_song_page(clip_id)
            identity = producer_identity_from_page(clip_id, html)
            urls, parsed = media_urls(html, clip_id)
            if parsed is not None and parsed.is_public is False:
                raise ProducerLookupError("own clip page marks this recording non-public")
        except ProducerLookupError:
            raise
        except Exception as exc:
            raise ProducerLookupError(f"could not inspect clip {clip_id}") from exc
        decisions[clip_id] = (identity, urls, parsed)
    result = []
    for track in tracks:
        clip_id = track.id.lower()
        identity, urls, parsed = decisions[clip_id]
        resolved = track.model_copy(deep=True)
        resolved.id = clip_id
        resolved.page_url = song_page_url(clip_id)
        resolved.artist = identity.artist
        resolved.creator_handle = identity.handle
        resolved.title = identity.title
        resolved.artist_is_fallback = False
        resolved.artist_is_ambiguous = False
        resolved.media_urls = list(urls)
        resolved.audio_url = next(
            (u for u in urls if u.split("?", 1)[0].lower().endswith((".m4a", ".mp3", ".wav"))),
            None,
        )
        # No caller-supplied cover location is allowed to survive verification.
        resolved.cover_url = parsed.cover_url if parsed else None
        if parsed and parsed.duration_sec:
            resolved.duration_sec = parsed.duration_sec
        result.append(resolved)
    return result
