"""HTML/RSC parsing for Suno profile, playlist, and song pages.

Suno renders pages as Next.js React Server Components: track data is embedded
in `self.__next_f.push(...)` script chunks containing escaped-JSON fragments.
We parse those first (richest source), then fall back to DOM anchors and meta
tags. Parsers skip malformed items rather than crashing, and log counts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .discovery import (
    _canonical_handle,
    _creator_from_feed_item,
    _identity_is_ambiguous,
    _identity_sources,
    _validated_identity,
)
from .models import TrackInfo
from .urls import (
    UUID_RE,
    cdn_cover_url,
    cdn_guess_urls,
    is_allowed_media_url,
    song_page_url,
)

logger = logging.getLogger(__name__)

# Escaped RSC JSON fragments: \"audio_url\":\"https://...\"
_ESCAPED_URL_RE = re.compile(r'\\"url\\":\\"(https?://[^\\"]+)\\"')
_NEXT_F_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')

# RSC chunk format: <id>:<type><data> where type is T (text), I (JSON), etc.
# e.g. 51:T7b8,[lyrics...] or 40:["$","$L50",null,{...}]
# These appear anywhere in the HTML (not necessarily at line starts), so no ^ anchor.
# Use a lookahead for the next chunk ID or end of string.
_RSC_CHUNK_RE = re.compile(r"(\d+):([TI])(.+?)(?=\d+:[TI]|</script>|$)")

# Upper bound on the concatenated RSC blob we are willing to scan. Real pages
# are ~100-200 KB; anything far larger is malformed or hostile, and scanning it
# repeatedly (once per clip candidate) would be a cheap DoS.
_MAX_BLOB_BYTES = 8 * 1024 * 1024


def _extract_rsc_chunks(html: str, *, reject_duplicate_ids: bool = False) -> dict[str, str]:
    """Extract all RSC chunks from HTML and return a mapping of chunk_id -> unescaped content.

    Handles both `self.__next_f.push([1,"..."])` format and the newer
    `<id>:<type><data>` format (e.g. `51:T7b8,[lyrics...]`).
    """
    chunks: dict[str, str] = {}

    # 1. Legacy self.__next_f.push format
    next_f_chunks = _NEXT_F_CHUNK_RE.findall(html)
    for chunk in next_f_chunks:
        unescaped = _unescape_js(chunk)
        # These don't have explicit IDs, so we concatenate them
        chunks["__next_f__"] = chunks.get("__next_f__", "") + unescaped

    # 2. Newer numbered chunk format: <id>:T<data> or <id>:I<json>
    seen_ids: set[str] = set()
    for m in _RSC_CHUNK_RE.finditer(html):
        chunk_id, chunk_type, data = m.groups()
        if reject_duplicate_ids and chunk_id in seen_ids:
            raise AmbiguousReferenceError("repeated RSC chunk ID")
        seen_ids.add(chunk_id)
        if chunk_type == "T":
            # Text chunk - data starts after a prefix like "T7b8,"
            # Find the first comma and take everything after it
            comma_idx = data.find(",")
            if comma_idx >= 0:
                content = data[comma_idx + 1 :]
                chunks[chunk_id] = _unescape_js(content)
        elif chunk_type == "I":
            # JSON chunk - the entire data is JSON
            with contextlib.suppress(Exception):
                # The data may be a JSON array, extract it
                chunks[chunk_id] = _unescape_js(data)

    return chunks


def _resolve_refs(obj: object, chunks: dict[str, str]) -> object:
    """Recursively resolve $<id> references in an object using the chunks map."""
    if isinstance(obj, str):
        # Check if the string is a reference like "$51"
        if obj.startswith("$") and obj[1:].isdigit():
            ref_id = obj[1:]
            if ref_id in chunks:
                return chunks[ref_id]
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_refs(v, chunks) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_refs(v, chunks) for v in obj]
    return obj


def _extract_next_f_blob(html: str) -> str:
    """Concatenate all React Flight chunks and unescape to embedded JSON text.

    Uses a targeted escape decoder instead of ``unicode_escape``, which (a)
    corrupts *raw* UTF-8 already present in the payload and (b) emits lone
    surrogates for emoji ``\\uD83C\\uDFB5`` pairs. Oversized blobs are rejected
    rather than scanned.
    """
    chunks = _NEXT_F_CHUNK_RE.findall(html)
    if not chunks:
        return ""
    blob = "".join(chunks)
    if len(blob) > _MAX_BLOB_BYTES:
        logger.warning("RSC blob too large (%d bytes); refusing to parse", len(blob))
        return ""
    return _unescape_js(blob)


# Escapes that can appear inside a JSON-in-a-JS-string payload.
_JS_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


def _unescape_js(blob: str) -> str:
    """Decode JS string escapes with correct unicode handling.

    Handles ``\\uXXXX`` (incl. surrogate pairs), ``\\xXX``, and the common
    single-character escapes. Unknown escapes are preserved verbatim.
    """
    if "\\" not in blob:
        return blob
    out: list[str] = []
    i = 0
    n = len(blob)
    while i < n:
        c = blob[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = blob[i + 1]
        if nxt in _JS_ESCAPES:
            out.append(_JS_ESCAPES[nxt])
            i += 2
        elif nxt == "u" and i + 5 < n:
            code = _hex4(blob, i + 2)
            if code is None:
                out.append(c)
                i += 1
                continue
            i += 6
            # Combine a high surrogate with a following low surrogate.
            if 0xD800 <= code <= 0xDBFF and blob[i : i + 2] == "\\u":
                low = _hex4(blob, i + 2)
                if low is not None and 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    i += 6
            out.append(chr(code) if not (0xD800 <= code <= 0xDFFF) else "\ufffd")
        elif nxt == "x" and i + 3 < n:
            code = _hex4(blob, i + 2, width=2)
            if code is None:
                out.append(c)
                i += 1
            else:
                out.append(chr(code))
                i += 4
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _hex4(s: str, start: int, width: int = 4) -> int | None:
    try:
        return int(s[start : start + width], 16)
    except ValueError:
        return None


class DuplicateClipKey(ValueError):
    """A candidate clip object contains ambiguous repeated JSON keys."""


class AmbiguousReferenceError(ValueError):
    """A referenced RSC chunk identifier is repeated."""


class MalformedClipObject(ValueError):
    """A candidate clip object cannot be examined safely."""


class ProducerIdentityConflict(ValueError):
    """Clip-local producer evidence is malformed or contradictory."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateClipKey(f"duplicate clip JSON field: {key}")
        result[key] = value
    return result


def _clip_objects(blob: str, *, reject_duplicate_keys: bool = False) -> Iterable[dict]:
    """Yield clip dicts from an RSC blob containing {\"clip\": {...}} pairs,
    or from plain JSON documents with clip-shaped dicts."""
    # Strategy: find every {...} object containing both "id" and "title" by
    # bracket-matching after each \"clip\": or \"content_item\": key.
    # Song pages now ship the clip as react-query ``"state":{"data":{...}}``
    # (``entity_type: song_schema``) instead of under a ``"clip"`` key; accept
    # ``"data"`` objects only when they are unmistakably a song, so playlist or
    # user payloads are never mistaken for clips.
    for key in ('"clip"', '"content_item"', '"data"'):
        for m in re.finditer(re.escape(key) + r"\s*:\s*\{", blob):
            obj = _brace_match(blob, m.end() - 1)
            if obj is None:
                if reject_duplicate_keys:
                    raise MalformedClipObject("unbalanced candidate object")
                continue
            try:
                data = json.loads(
                    obj, object_pairs_hook=_unique_object if reject_duplicate_keys else None
                )
            except json.JSONDecodeError as exc:
                if reject_duplicate_keys:
                    raise MalformedClipObject("malformed candidate object") from exc
                continue
            if not (isinstance(data, dict) and "id" in data and "title" in data):
                continue
            if key == '"data"' and data.get("entity_type") != "song_schema":
                continue
            yield data


def _matching_clip_producer(
    candidates: list[dict], chunks: dict[str, str], clip_id: str
) -> tuple[bool, str | None]:
    """Reconcile every matching candidate without joining unproven identities.

    Only literal producer objects and references resolving to complete JSON
    objects are supported. An unknown producer is returned only if every
    matching candidate has no producer fields. This is not a full RSC parser.
    """
    fields: dict[str, set[str]] = {"display_name": set(), "handle": set()}
    producers: set[str] = set()
    found = unknown = False
    for raw in candidates:
        if str(raw.get("id", "")).lower() != clip_id:
            continue
        found = True
        obj = dict(raw)
        for key in ("user", "author", "creator", "profile"):
            value = obj.get(key)
            if isinstance(value, str) and re.fullmatch(r"\$[0-9]+", value):
                content = chunks.get(value[1:])
                if content is None:
                    raise ProducerIdentityConflict("unresolved producer reference")
                try:
                    value = json.loads(content, object_pairs_hook=_unique_object)
                except (ValueError, TypeError) as exc:
                    raise ProducerIdentityConflict("malformed producer reference") from exc
                obj[key] = value
            if value is not None and not isinstance(value, dict):
                raise ProducerIdentityConflict("unsupported producer object")
        resolved = _resolve_refs(obj, chunks)
        if not isinstance(resolved, dict):
            raise ProducerIdentityConflict("invalid producer object")
        if _identity_is_ambiguous(resolved):
            raise ProducerIdentityConflict("conflicting clip-local identities")
        sources = _identity_sources(resolved)
        identity_sources = [
            source for source in sources if any(field in source for field in fields)
        ]
        if len(identity_sources) > 1:
            raise ProducerIdentityConflict("producer fields from multiple objects are unproven")
        if not identity_sources:
            unknown = True
            continue
        source = identity_sources[0]
        for field in fields:
            if field in source:
                raw_value = source[field]
                if isinstance(raw_value, str) and raw_value.startswith("$"):
                    raise ProducerIdentityConflict("unresolved producer field")
                value = _validated_identity(source, (field,))
                if value is None:
                    raise ProducerIdentityConflict("malformed clip-local identity")
                fields[field].add(value)
                if len(fields[field]) > 1:
                    raise ProducerIdentityConflict("conflicting matching clip identities")
        producer = _validated_identity(source, ("display_name", "handle"))
        if producer is None:
            raise ProducerIdentityConflict("missing producer")
        producers.add(producer)
    if len(producers) > 1 or (unknown and producers):
        raise ProducerIdentityConflict("conflicting or incomplete matching clip identities")
    return found, next(iter(producers), None)


def _brace_match(s: str, start: int) -> str | None:
    """Return the balanced {...} substring starting at position start."""
    if start >= len(s) or s[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _cdn_media_urls(clip_id: str) -> tuple[str, str]:
    """Last-resort CDN guesses (cdn1 objects are usually permissioned now)."""
    return (cdn_guess_urls(clip_id)[0], cdn_cover_url(clip_id))


def _extract_media_urls(data: dict, clip_id: str) -> list[str]:
    """Collect candidate public media URLs for a clip, best-first.

    Suno RSC payloads embed ``media_urls`` and per-field ``*_url`` entries.
    The canonical ``audio_url`` is usually the ``/api/forbidden`` sentinel, and
    cdn1 ``.mp3`` objects are permissioned. Only URLs on allow-listed hosts are
    kept, so a hostile payload cannot point the downloader at internal hosts.
    """
    urls: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str) or not value.startswith("http"):
            return
        if "forbidden" in value or "/api/" in value:
            return
        if not is_allowed_media_url(value):
            logger.debug("Discarding non-allow-listed media host: %s", value.split("?", 1)[0])
            return
        if value not in urls:
            urls.append(value)

    media = data.get("media_urls") or data.get("mediaUrls")
    if isinstance(media, list):
        for entry in media:
            if isinstance(entry, dict):
                for key in ("url", "mp3_url", "wav_url", "video_url"):
                    add(entry.get(key))
            else:
                add(entry)

    for key in ("mp3_url", "audio_url", "video_url", "wav_url"):
        add(data.get(key))

    # Last-resort CDN guesses (usually 403; real objects come from media_urls).
    for guess in cdn_guess_urls(clip_id):
        add(guess)
    return urls


def _opt_str(value: object) -> str | None:
    """A non-empty string, else None (metadata fields are best-effort)."""
    return value if isinstance(value, str) and value else None


def _clip_to_track(data: dict, default_handle: str | None) -> TrackInfo | None:
    clip_id = str(data.get("id") or "")
    if not UUID_RE.fullmatch(clip_id):
        return None
    title = str(data.get("title") or "Untitled")

    media_urls = _extract_media_urls(data, clip_id)
    audio_url = media_urls[0] if media_urls else None

    cover_url = None
    for field in ("image_large_url", "image_url"):
        v = data.get(field)
        if isinstance(v, str) and v.startswith("http"):
            cover_url = v
            break
    if cover_url is None:
        cover_url = cdn_cover_url(clip_id)

    duration = data.get("duration")
    try:
        duration_sec = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError, OverflowError):
        duration_sec = None

    # The source profile is not proof of a clip's creator. Reuse the feed's
    # identity validation for RSC clip-local metadata, including unsafe
    # controls and ambiguous/invalid explicit creator fields.
    artist = _creator_from_feed_item(data, default_handle or "")
    creator_handle = _canonical_handle(data)
    created_at = data.get("created_at") or data.get("createdAt")

    # Extract lyrics from metadata.prompt if present
    lyrics: str | None = None
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        prompt = metadata.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            lyrics = prompt.strip()

    style_tags: list[str] = []
    for key in ("tags", "style_tags", "styleTags"):
        value = data.get(key)
        if isinstance(value, list):
            style_tags = [str(t) for t in value if isinstance(t, str)]
            break
        elif isinstance(value, str):
            style_tags = [value]
            break

    play_count = data.get("play_count") or data.get("playCount")
    try:
        play_count = int(play_count) if play_count is not None else None
    except (TypeError, ValueError, OverflowError):
        play_count = None

    model_version = data.get("major_model_version") or data.get("model_name")
    meta: dict = metadata if isinstance(metadata, dict) else {}
    return TrackInfo(
        id=clip_id,
        title=title,
        artist=artist,
        artist_is_ambiguous=_identity_is_ambiguous(data),
        artist_is_fallback=bool(
            artist and default_handle and not _creator_from_feed_item(data, "")
        ),
        creator_handle=creator_handle,
        duration_sec=duration_sec,
        page_url=song_page_url(clip_id),
        audio_url=audio_url,
        cover_url=cover_url,
        media_urls=media_urls,
        created_at=created_at if isinstance(created_at, str) else None,
        lyrics=lyrics,
        style_tags=style_tags,
        play_count=play_count,
        model_version=model_version if isinstance(model_version, str) else None,
        model_name=_opt_str(data.get("model_name")),
        tags=_opt_str(data.get("tags") or meta.get("tags")),
        is_public=data["is_public"] if isinstance(data.get("is_public"), bool) else None,
    )


def parse_profile(html: str, handle: str = "") -> list[TrackInfo]:
    """Parse a public profile page; returns deduplicated tracks (order preserved)."""
    return _parse_track_list(html, default_handle=handle.lstrip("@") or None)


def parse_playlist(html: str) -> list[TrackInfo]:
    tracks = _parse_track_list(html, default_handle=None)
    for track in tracks:
        track.from_playlist = True
    return tracks


def _trusted_dom_song_id(href: str) -> str | None:
    """Accept only direct Suno song links, never arbitrary UUIDs in an href."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if (parts.scheme or parts.netloc) and (
        parts.scheme != "https" or parts.netloc.lower() not in {"suno.com", "www.suno.com"}
    ):
        return None
    match = re.fullmatch(r"/song/(" + UUID_RE.pattern + r")/?", parts.path)
    return match.group(1).lower() if match else None


def _parse_track_list(html: str, default_handle: str | None) -> list[TrackInfo]:
    tracks: dict[str, TrackInfo] = {}

    # 1. RSC embedded clip objects (richest: titles, durations, audio URLs).
    # Extract all RSC chunks first to resolve $<id> references (e.g. lyrics).
    rsc_chunks = _extract_rsc_chunks(html)
    blob = _extract_next_f_blob(html)
    if blob:
        for clip in _clip_objects(blob):
            # Resolve any $<id> references in the clip data
            resolved = _resolve_refs(clip, rsc_chunks)
            clip = resolved if isinstance(resolved, dict) else clip
            try:
                track = _clip_to_track(clip, default_handle)
            except Exception as exc:  # noqa: BLE001 — skip malformed item
                logger.warning("Skipping unparseable clip: %s", exc)
                continue
            if track and track.id not in tracks:
                tracks[track.id] = track

    # 2. Fallback: DOM anchors linking to /song/<uuid> (no titles guarantee).
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href*="/song/"]'):
        href = a.get("href")
        clip_id = _trusted_dom_song_id(href) if isinstance(href, str) else None
        if clip_id is None:
            continue
        if clip_id in tracks:
            continue
        title = " ".join(a.get_text(" ", strip=True).split()) or "Untitled"
        audio_url, cover_url = _cdn_media_urls(clip_id)
        tracks[clip_id] = TrackInfo(
            id=clip_id,
            title=title,
            # A DOM anchor has no clip-local creator identity. Keep the
            # profile handle as source provenance in the caller/sidecar only.
            artist=None,
            page_url=song_page_url(clip_id),
            audio_url=audio_url,
            cover_url=cover_url,
        )

    logger.info("Parsed %d tracks", len(tracks))
    return list(tracks.values())


def parse_song(html: str) -> list[TrackInfo]:
    """Parse a single song page (returns 0–1 track)."""
    rsc_chunks = _extract_rsc_chunks(html)
    blob = _extract_next_f_blob(html)
    if blob:
        for clip in _clip_objects(blob):
            resolved = _resolve_refs(clip, rsc_chunks)
            clip = resolved if isinstance(resolved, dict) else clip
            track = _clip_to_track(clip, default_handle=None)
            if track:
                logger.info("Parsed song %s (%s)", track.id, track.title)
                return [track]

    soup = BeautifulSoup(html, "html.parser")

    # og:meta fallback
    og_title = soup.find("meta", property="og:title")
    og_audio = soup.find("meta", property="og:audio")
    canonical = soup.find("link", rel="canonical")
    page_url = str(canonical.get("href")) if canonical and canonical.get("href") else ""
    # Prefer direct trusted canonical identity. A UUID in arbitrary HTML or
    # an og:image asset is not proof of the song's own ID.
    if page_url:
        clip_id = _trusted_dom_song_id(page_url)
    else:
        # Older minimal pages have only a song link. Accept that fallback
        # only when every trusted link identifies the same single clip.
        ids: set[str] = set()
        for anchor in soup.select('a[href*="/song/"]'):
            href = anchor.get("href")
            candidate = _trusted_dom_song_id(href) if isinstance(href, str) else None
            if candidate is not None:
                ids.add(candidate)
        clip_id = next(iter(ids)) if len(ids) == 1 else None
    if clip_id is None:
        logger.warning("Song page has no unambiguous trusted clip id; skipping")
        return []

    og_title_raw = og_title.get("content") if og_title else None
    title = og_title_raw or "Untitled"
    audio_url = og_audio.get("content") if og_audio else None
    if not (isinstance(audio_url, str) and audio_url.startswith("http")):
        audio_url = None
    if audio_url is None or "forbidden" in audio_url:
        audio_url = cdn_guess_urls(clip_id)[0]

    og_image = soup.find("meta", property="og:image")
    cover_url = og_image.get("content") if og_image else None
    if not (isinstance(cover_url, str) and cover_url.startswith("http")):
        cover_url = cdn_cover_url(clip_id)

    track = TrackInfo(
        id=clip_id,
        title=str(title),
        page_url=song_page_url(clip_id),
        audio_url=audio_url,
        cover_url=cover_url,
    )
    logger.info("Parsed song %s (%s)", track.id, track.title)
    return [track]
