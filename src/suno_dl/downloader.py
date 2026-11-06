"""Concurrent downloading of Suno public tracks with retry and resume."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from tqdm import tqdm

from .archive_lock import archive_locked_if_capped, raise_if_unsupported_archive_write
from .download_intent import (
    UnsafeDownloadIntent,
    complete_without_sidecar,
    completed_owner,
    has_reserved_base,
    sync_directory,
)
from .download_intent import (
    prepare as prepare_intent,
)
from .download_intent import (
    reserve as reserve_intent,
)
from .errors import BlockedError, DiskSpaceError, ResolutionError
from .fetcher import Fetcher
from .ffmpeg import find_ffmpeg
from .library import LibraryIndex, probe_media, record_membership
from .linux_downloads import require_audio_sync
from .media_process import run as run_media_process
from .metadata import reconcile
from .models import TrackInfo
from .naming import build_filename, pattern_uses_artist, sanitize_component
from .pacing import PlaybackPacer
from .parser import parse_song
from .producer_lookup import _require_trusted_framing, verify_track_producers
from .rotate import (
    MEGABYTE,
    RotationState,
    archive_usage,
    recover_rotation,
    rotate_archive,
)
from .sidecar import (
    build_sidecar,
    read_sidecar,
    saved_files_for,
    sidecar_path,
    write_sidecar,
)
from .tagging import (
    require_embedded_tag_writes,
    tag_file,
    write_lyrics_sidecar,
)
from .urls import (
    GUESS_SUFFIXES,
    is_allowed_media_url,
    redact_url,
    song_page_url,
)

logger = logging.getLogger(__name__)


def _display_text(value: object) -> str:
    """Escape characters that can forge or visually reorder output."""
    return "".join(
        (f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
        if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
        else char
        for char in str(value)
    )


PUBLIC_REFERER = "https://suno.com"

# Suffixes, in preference order, tried when naming an output file. ``wav`` is
# not a CDN guess (cdn1 never served wav) but real wav objects appear in
# ``media_urls``, and they must not be mislabelled ``.mp3``.
_MEDIA_SUFFIXES = tuple("." + s for s in (*GUESS_SUFFIXES, "wav"))

# Cover-art extensions; a URL ending in one of these is an image, not audio.
_IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png")

# Absolute ceiling for one cover image, independent of any archive byte cap:
# a hostile or misconfigured endpoint must not be able to stream gigabytes.
MAX_COVER_BYTES = 8 * 1024 * 1024


def _check_disk_space(path: Path, min_free_mb: int) -> None:
    """Check if the volume containing ``path`` has at least ``min_free_mb`` free.

    Raises ``DiskSpaceError`` if free space is below the threshold.
    """
    if min_free_mb <= 0:
        return
    free_bytes = shutil.disk_usage(path).free
    free_mb = free_bytes // (1024 * 1024)
    if free_mb < min_free_mb:
        raise DiskSpaceError(
            f"Insufficient free space on {path}: {free_mb} MB available, {min_free_mb} MB required"
        )


def _compress_image(path: Path) -> None:
    """Recompress ``path`` in place if that actually shrinks it.

    Requires Pillow (the optional ``image`` extra); when it is not installed
    this is a no-op — covers are simply kept as served, never an error. The
    original file is replaced only when the recompressed copy is strictly
    smaller, so the outcome is never worse than skipping compression.
    """
    try:
        from PIL import Image  # lazy: optional [image] extra
    except ImportError:
        logger.debug("Pillow not installed; keeping cover as served: %s", _display_text(path.name))
        return
    if path.suffix.lower() not in (".jpg", ".jpeg"):
        # Re-encoding a PNG as JPEG would write JPEG bytes under a .png name.
        # Keeping the original is the "never worse than skipping" guarantee.
        logger.debug("Cover is not a JPEG; keeping as served: %s", _display_text(path.name))
        return
    import io

    try:
        with Image.open(path) as im:
            im.load()
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85, optimize=True)
    except Exception as exc:  # noqa: BLE001 — bad image data must not fail a run
        logger.debug(
            "Cover recompression skipped for %s (%s)", _display_text(path.name), type(exc).__name__
        )
        return
    if buf.tell() < path.stat().st_size:
        path.write_bytes(buf.getvalue())
        logger.debug(
            "Recompressed cover %s (%d bytes saved)", _display_text(path.name), path.stat().st_size
        )


def _safe_transfer_error(exc: Exception) -> str:
    """Classify transfer failures without printing an untrusted message."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    if isinstance(exc, httpx.RequestError):
        return f"request error ({type(exc).__name__})"
    if isinstance(exc, OSError):
        return "local I/O error"
    return type(exc).__name__


async def _download_cover(
    track: TrackInfo,
    out_base: Path,
    output_dir: Path,
    fetcher: Fetcher,
    budget: DownloadBudget | None = None,
) -> Path | None:
    """Best-effort cover-art download (CLI default on via ``--covers``).

    Never fails the run: any error is logged (redacted) and the cover is
    simply skipped. Output name is ``<out_base>.cover.<ext>`` so it always
    sorts beside its track and cannot collide with a media file.
    """
    cover = track.cover_url
    if not cover or not is_allowed_media_url(cover):
        logger.debug("No allow-listed cover URL for %s; skipping", track.id)
        return None
    suffix = ".jpg"
    lowered = cover.lower().split("?", 1)[0]
    for ext in _IMAGE_SUFFIXES:
        if lowered.endswith(ext):
            suffix = ext
            break
    target_path = Path(str(out_base) + ".cover" + suffix)
    if target_path.exists():
        return None
    if not _is_contained(target_path, output_dir):
        logger.error("Refusing to write outside output dir: %s", _display_text(target_path.name))
        return None
    tmp_path: Path | None = None
    available = (
        max(0, budget.bytes_left - 4096)
        if budget is not None and budget.bytes_left is not None
        else None
    )
    if available is not None and available <= 0:
        logger.info("Skipping cover at archive byte cap: %s", track.id)
        return None
    try:
        await fetcher.rate_limiter.acquire(cover)
        async with fetcher.client.stream("GET", cover) as resp:
            if resp.status_code in (401, 403, 404):
                logger.debug(
                    "Cover unavailable for %s: %s (%d)",
                    track.id,
                    _display_text(redact_url(cover)),
                    resp.status_code,
                )
                return None
            resp.raise_for_status()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_path.parent,
                prefix=".suno-cover-",
                suffix=".part",
                delete=False,
            ) as fh:
                tmp_path = Path(fh.name)
                written = 0
                async for chunk in resp.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if available is not None and written > available:
                        raise _PairedMediaCapReached
                    if written > MAX_COVER_BYTES:
                        raise _CoverTooLarge
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        if tmp_path is None:
            raise OSError("temporary download file was not created")
        if tmp_path.stat().st_size > 0:
            os.chmod(tmp_path, 0o644)
            os.link(tmp_path, target_path, follow_symlinks=False)
            tmp_path.unlink()
            tmp_path = None
            _compress_image(target_path)
            if budget is not None:
                budget.spend(target_path.stat().st_size, new_track=False)
            return target_path
    except _PairedMediaCapReached:
        logger.info("Skipping oversized cover at archive byte cap: %s", track.id)
    except _CoverTooLarge:
        logger.warning("Refusing cover larger than %d bytes for %s", MAX_COVER_BYTES, track.id)
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        logger.debug(
            "Cover fetch failed for %s at %s: %s",
            track.id,
            _display_text(redact_url(cover)),
            _safe_transfer_error(exc),
        )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return None


def _looks_like_media(payload: bytes) -> bool:
    """Structural sniff: does this byte prefix look like a media container?

    Suno's primary cloudfront URL serves an *encrypted* payload with HTTP 200
    and ``audio/mp4``, which size-only validation happily saved as unplayable
    junk. We accept only recognizable containers: MP4/ISOBMFF (``ftyp`` box at
    offset 4), MP3 (ID3 tag or frame sync), or RIFF/WAVE. Anything else —
    encrypted blobs, XML error pages served with 200, HTML — is rejected so
    the caller falls through to the next candidate.
    """
    if len(payload) < 12:
        return False
    if payload[4:8] == b"ftyp":
        return True
    if payload[:3] == b"ID3":
        return True
    if payload[0] == 0xFF and (payload[1] & 0xE0) == 0xE0:
        return True
    return payload[:4] == b"RIFF" and payload[8:12] == b"WAVE"


def _suffix_for(url: str) -> str:
    """Pick an output extension from a media URL, ignoring query strings."""
    path = url.lower().split("?", 1)[0].split("#", 1)[0]
    for ext in _MEDIA_SUFFIXES:
        if path.endswith(ext):
            return ext
    return ".mp3"


def _media_candidates(track: TrackInfo, preferred: str | None = None) -> list[str]:
    """Candidate media URLs for a track, allow-list filtered.

    When ``preferred`` is given (e.g. ``m4a``), URLs with that extension are
    tried first; all others remain as fallbacks.
    """
    seen: list[str] = []
    for url in [track.audio_url, *track.media_urls]:
        if url and url not in seen:
            seen.append(url)
    allowed = [u for u in seen if is_allowed_media_url(u)]
    if preferred:
        want = "." + preferred.lower().lstrip(".")
        allowed.sort(key=lambda u: 0 if u.split("?", 1)[0].lower().endswith(want) else 1)
    return allowed


async def _resolve_media_urls(
    track: TrackInfo, fetcher: Fetcher, *, resolve_artist: bool = False
) -> list[str]:
    """Return the track's real media URLs, fetching its song page if needed.

    Profile discovery only yields clip IDs; the playable objects live in each
    song page's RSC payload (CloudFront ``media_urls``, often signed). Any URL
    that is not on an allow-listed host is discarded.
    """
    if resolve_artist:
        verified = (await verify_track_producers([track], fetcher))[0]
        for field in (
            "artist",
            "creator_handle",
            "title",
            "page_url",
            "audio_url",
            "media_urls",
            "artist_is_fallback",
            "artist_is_ambiguous",
            "cover_url",
        ):
            setattr(track, field, getattr(verified, field))
        return list(verified.media_urls)
    resolved = [u for u in track.media_urls if is_allowed_media_url(u)]
    if resolved and not (resolve_artist and (not track.artist or track.artist_is_fallback)):
        return resolved
    # ``page_url`` may come from a hostile page's <link rel=canonical>; never
    # fetch an unvalidated URL. Fall back to the canonical song URL built from
    # the (always-UUID) track id instead of losing the track.
    page_url = track.page_url if is_allowed_media_url(track.page_url) else song_page_url(track.id)
    try:
        html = await fetcher.fetch_html(page_url)
    except Exception as exc:  # noqa: BLE001 — network variance; fall back below
        raise ResolutionError(
            f"could not fetch {_display_text(redact_url(page_url))} ({type(exc).__name__})"
        ) from None
    try:
        parsed_tracks = parse_song(html)
    except Exception as exc:  # noqa: BLE001 - malformed public page must not abort the batch
        raise ResolutionError(f"could not parse song page ({exc.__class__.__name__})") from exc
    for parsed in parsed_tracks:
        if parsed.id.lower() == track.id.lower():
            # The song page is the richest source for this track: keep its
            # creation timestamp when the discovery stage had none, so
            # rotation can order by Suno's clock instead of mtime (#93).
            if parsed.created_at and not track.created_at:
                track.created_at = parsed.created_at
            if resolve_artist:
                if (not track.artist or track.artist_is_fallback) and parsed.artist:
                    track.artist = parsed.artist
                    track.artist_is_fallback = False
                if not track.creator_handle and parsed.creator_handle:
                    track.creator_handle = parsed.creator_handle
            return [u for u in parsed.media_urls if is_allowed_media_url(u)] or resolved
    return resolved


def _is_contained(path: Path, root: Path) -> bool:
    """True if ``path`` resolves to a direct or nested child of ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@dataclass
class DownloadBudget:
    """Rotate-off caps: stop *downloading* at the cap, never delete (#93).

    ``tracks_left``/``bytes_left`` are seeded from the archive as it is before
    the run starts and spent as new tracks land. Downloads are concurrent, so
    a few in-flight tasks can pass the check before the first spend: the cap
    is enforced to within one download, never by deleting anything.
    """

    tracks_left: int | None = None
    bytes_left: int | None = None

    def allows(self, *, existing_track: bool = False) -> bool:
        if not existing_track and self.tracks_left is not None and self.tracks_left <= 0:
            return False
        return not (self.bytes_left is not None and self.bytes_left <= 0)

    def spend(self, written: int, *, new_track: bool = True) -> None:
        if new_track and self.tracks_left is not None:
            self.tracks_left -= 1
        if self.bytes_left is not None:
            self.bytes_left = max(0, self.bytes_left - written)


def _sidecar_bytes(out_base: Path, media_path: Path) -> int:
    """Bytes a freshly downloaded track added, covers included."""
    total = 0
    with contextlib.suppress(OSError):
        total += media_path.stat().st_size
    try:
        covers_on_disk = list(out_base.parent.glob(out_base.name + ".cover.*"))
    except OSError:
        covers_on_disk = []
    for cover in covers_on_disk:
        with contextlib.suppress(OSError):
            total += cover.stat().st_size
    return total


def _remux_without_overwrite(src: Path, dest: Path, ffmpeg: str) -> bool:
    """Remux privately and atomically publish only if the destination is free."""
    if dest.exists() or dest.is_symlink():
        return False
    require_embedded_tag_writes()
    try:
        with tempfile.TemporaryDirectory(prefix=".suno-remux-", dir=src.parent) as directory:
            staged = Path(directory) / "audio.m4a"
            cmd = [ffmpeg, "-v", "error", "-i", str(src), "-vn", "-c:a", "copy", str(staged)]
            run_media_process(cmd, check=True, capture_output=True, timeout=300)
            if not staged.is_file() or staged.stat().st_size <= 0:
                return False
            os.chmod(staged, 0o644)
            os.link(staged, dest, follow_symlinks=False)
            return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning(
            "audio extraction failed for %s (%s)", _display_text(src.name), type(exc).__name__
        )
        return False


async def _extract_audio_from_mp4(src: Path, dest: Path) -> bool:
    """Extract audio from MP4 to M4A without overwriting an existing file."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning("--extract-audio requested but ffmpeg not found on PATH; skipping")
        return False
    return _remux_without_overwrite(src, dest, ffmpeg)


class _PairedMediaCapReached(Exception):
    """Stop an in-flight video before its paired audio exceeds the byte budget."""


class _CoverTooLarge(Exception):
    """Stop a cover transfer that exceeds the absolute per-image ceiling."""


_PAIR_OVERHEAD_BYTES = 64 * 1024


async def _try_download_candidate(
    media_url: str,
    track: TrackInfo,
    out_base: Path,
    output_dir: Path,
    fetcher: Fetcher,
    stats: dict[str, int],
    bar: tqdm | None,
    handle: str,
    covers: bool,
    extract_audio: bool = False,
    lyrics_sidecar: str | None = None,
    budget: DownloadBudget | None = None,
    sidecar: bool = False,
    keep_video: bool = False,
    existing_track: bool = False,
    replace_existing: bool = False,
    sidecars_written: set[tuple[str, Path]] | None = None,
    published_media: dict[str, set[str]] | None = None,
    replacement_paths: dict[Path, tuple[int, int, int, int]] | None = None,
    preserved_members: dict[Path, tuple[int, int, int, int]] | None = None,
) -> Literal["downloaded", "failed", "skipped_capped"]:
    """Attempt one media URL; preserve MP4 when video+audio was requested.

    Audio-only extraction continues to remove the temporary MP4. When
    ``keep_video`` is set, the original MP4 survives even if remux fails.

    ``replace_existing`` (#167) is the explicit force-redownload path: the new
    bytes are streamed to ``.part`` and only replace the old recording once
    they validate as a media container, so a failed transfer (or a DNS/HTTP
    error) leaves the previous, playable file untouched.
    """
    target_suffix = _suffix_for(media_url)
    target_path = Path(str(out_base) + target_suffix)
    replacement_identity = None
    if replace_existing:
        if (
            replacement_paths is None
            or target_path not in replacement_paths
            or target_path.is_symlink()
        ):
            logger.warning("Refusing unverified replacement target for %s", track.id)
            return "failed"
        replacement_identity = replacement_paths[target_path]
        try:
            info = target_path.stat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != replacement_identity:
                logger.warning("Replacement target changed since indexing for %s", track.id)
                return "failed"
        except OSError:
            return "failed"
    paired = keep_video and extract_audio and target_suffix == ".mp4"
    already_audio = (
        paired
        and target_path.with_suffix(".m4a").is_file()
        and (target_path.with_suffix(".m4a").stat().st_size > 1024)
    )
    paired_factor = 1 if already_audio else 2
    bytes_allowed = budget.bytes_left if paired and budget is not None else None
    if not _is_contained(target_path, output_dir):
        logger.error("Refusing to write outside output dir: %s", _display_text(target_path.name))
        return "failed"
    tmp_path: Path | None = None
    headers = {
        "Referer": PUBLIC_REFERER,
        "Origin": PUBLIC_REFERER,
        "User-Agent": fetcher.client.headers.get("User-Agent", ""),
    }
    try:
        await fetcher.rate_limiter.acquire(media_url)
        async with fetcher.client.stream("GET", media_url, headers=headers) as resp:
            if resp.status_code in (401, 403, 404):
                logger.debug(
                    "Media unavailable: %s (%d)",
                    _display_text(redact_url(media_url)),
                    resp.status_code,
                )
                return "failed"
            resp.raise_for_status()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target_path.parent, prefix=".suno-", suffix=".part", delete=False
            ) as fh:
                tmp_path = Path(fh.name)
                transferred = 0
                async for chunk in resp.aiter_bytes(64 * 1024):
                    transferred += len(chunk)
                    if (
                        bytes_allowed is not None
                        and transferred * paired_factor + _PAIR_OVERHEAD_BYTES > bytes_allowed
                    ):
                        raise _PairedMediaCapReached
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        if tmp_path is None:
            raise OSError("temporary download file was not created")
        if tmp_path.stat().st_size >= 1024 and _looks_like_media(tmp_path.read_bytes()[:32]):
            os.chmod(tmp_path, 0o644)
            if replace_existing:
                if target_path.is_symlink():
                    return "failed"
                try:
                    current = target_path.stat()
                except OSError:
                    return "failed"
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != replacement_identity:
                    logger.warning("Replacement target changed during download for %s", track.id)
                    return "failed"
                tmp_path.replace(target_path)
                stats["replaced"] = stats.get("replaced", 0) + 1
            else:
                # Atomic no-replace publication: another file always wins.
                os.link(tmp_path, target_path, follow_symlinks=False)
                tmp_path.unlink()
            tmp_path = None
            stats["downloaded"] += 1
            if bar:
                bar.update(1)
            _write_tags(target_path, track, handle, stats, lyrics_sidecar)
            owned_names = {target_path.name}
            if covers and not keep_video:
                cover_path = await _download_cover(track, out_base, output_dir, fetcher)
                if cover_path is not None:
                    owned_names.add(cover_path.name)

            # An explicit video+audio sync keeps the MP4 alongside its remuxed
            # M4A. The audio-only fallback continues to delete the temporary MP4.
            if extract_audio and target_path.suffix.lower() == ".mp4":
                m4a_path = target_path.with_suffix(".m4a")
                existing_audio = m4a_path.is_file() and m4a_path.stat().st_size > 1024
                if existing_audio and not existing_track:
                    # The path appeared after this track was scheduled. Never
                    # tag or claim a recording whose UUID was not established.
                    target_path.unlink(missing_ok=True)
                    stats["downloaded"] -= 1
                    return "failed"
                extracted = existing_audio or await _extract_audio_from_mp4(target_path, m4a_path)
                if extracted:
                    owned_names.add(m4a_path.name)
                    if not keep_video:
                        target_path.unlink(missing_ok=True)
                        owned_names.discard(target_path.name)
                    if not (keep_video and existing_audio):
                        _write_tags(m4a_path, track, handle, stats, lyrics_sidecar)
                    if not keep_video:
                        if sidecar:
                            wrote_sidecar = _record_sidecar(
                                track,
                                out_base,
                                output_dir,
                                handle,
                                stats,
                                overwrite=replace_existing or existing_track,
                                source="download",
                                owned_names=owned_names,
                                preserved_members=preserved_members,
                            )
                            if wrote_sidecar and sidecars_written is not None:
                                sidecars_written.add((track.id.lower(), sidecar_path(out_base)))
                        if budget is not None:
                            budget.spend(_sidecar_bytes(out_base, m4a_path))
                        if published_media is not None:
                            published_media[track.id.lower()] = {
                                n for n in owned_names if Path(n).suffix.lower() in _MEDIA_SUFFIXES
                            }
                        return "downloaded"
                elif not keep_video:
                    # The audio-only fallback must never leave a video artifact.
                    target_path.unlink(missing_ok=True)
                    stats["downloaded"] -= 1
                    return "failed"
                else:
                    logger.warning("Could not extract M4A for %s; retaining MP4", track.id)

            if sidecar:
                wrote_sidecar = _record_sidecar(
                    track,
                    out_base,
                    output_dir,
                    handle,
                    stats,
                    overwrite=replace_existing or existing_track,
                    source="download",
                    owned_names=owned_names,
                    preserved_members=preserved_members,
                )
                if wrote_sidecar and sidecars_written is not None:
                    sidecars_written.add((track.id.lower(), sidecar_path(out_base)))
            if published_media is not None:
                published_media[track.id.lower()] = {
                    n for n in owned_names if Path(n).suffix.lower() in _MEDIA_SUFFIXES
                }
            if budget is not None:
                extra_audio = 0
                if keep_video and extract_audio and target_path.suffix.lower() == ".mp4":
                    audio_path = target_path.with_suffix(".m4a")
                    if audio_path.is_file() and not existing_audio:
                        extra_audio = audio_path.stat().st_size
                if keep_video:
                    # Count new media only: existing M4A and covers are already
                    # accounted for at archive_usage before this sync.
                    budget.spend(
                        target_path.stat().st_size + extra_audio,
                        new_track=not existing_track,
                    )
                else:
                    budget.spend(_sidecar_bytes(out_base, target_path))
            if covers and keep_video:
                await _download_cover(track, out_base, output_dir, fetcher, budget)
            return "downloaded"
        logger.warning(
            "Media candidate failed validation: %s (encrypted or not a "
            "media container; trying next candidate)",
            _display_text(redact_url(media_url)),
        )
        return "failed"
    except BlockedError as exc:
        logger.debug(
            "Blocked candidate for %s at %s (%s)",
            track.id,
            _display_text(redact_url(media_url)),
            type(exc).__name__,
        )
        return "failed"
    except _PairedMediaCapReached:
        stats["skipped_capped"] += 1
        logger.info("Paired video/audio exceeds the nonrotating disk cap: %s", track.id)
        return "skipped_capped"
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        logger.debug(
            "Media candidate failed for %s at %s: %s",
            track.id,
            _display_text(redact_url(media_url)),
            _safe_transfer_error(exc),
        )
        return "failed"
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _unique_base(base: Path, taken: set[Path]) -> Path:
    """Return ``base`` or ``base_N`` avoiding *in-run* collisions only.

    On-disk files are deliberately NOT treated as collisions: an existing
    ``<base>.m4a`` is either a completed download of this same track (resume —
    the downloader will skip it) or a genuine name clash that the ``taken``
    set already covers.
    """
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = Path(f"{base}_{n}")
        if candidate not in taken:
            return candidate
    return Path(f"{base}_dup")


def _producer_unique_base(
    base: Path, taken: set[Path], clip_id: str, index: LibraryIndex, archive_root: Path
) -> Path | None:
    """Choose a producer basename without reusing another recording's files."""
    suffixes = (
        *_MEDIA_SUFFIXES,
        ".json",
        ".cover.jpg",
        ".cover.jpeg",
        ".cover.png",
        ".lrc",
        ".txt",
    )
    names = {p.name.casefold(): p for p in base.parent.iterdir()}
    reserved = {p.name.casefold() for p in taken}

    def occupied(candidate: Path) -> bool:
        stem = candidate.name.casefold()
        return stem in reserved or any((stem + ext).casefold() in names for ext in suffixes)

    stem = base.name.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(" ._") or "untitled"
    candidate = base.with_name(stem)
    if not occupied(candidate):
        return candidate
    if candidate.name.casefold() not in reserved:
        # An unindexed file could be our own interrupted download. Refuse to
        # guess its identity or create a second physical copy.
        sidecar = names.get((candidate.name + ".json").casefold())
        if sidecar is None:
            try:
                owner_id = completed_owner(archive_root, candidate)
            except UnsafeDownloadIntent:
                return None
            if owner_id is None or owner_id == clip_id.lower():
                return None
        else:
            if sidecar.is_symlink():
                return None
            claim = read_sidecar(sidecar)
            owner = index.get(str(claim["id"])) if claim is not None else None
            if owner is None or owner.base != sidecar.with_suffix(""):
                return None
            if owner.clip_id == clip_id.lower():
                return None
    token = "_" + sanitize_component(clip_id)
    budget = max(1, 180 - len(token.encode("utf-8")))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" ._") or "untitled"
    candidate = base.with_name(stem + token)
    return None if occupied(candidate) else candidate


def _write_tags(
    path: Path,
    track: TrackInfo,
    handle: str,
    stats: dict[str, int],
    lyrics_sidecar: str | None = None,
) -> None:
    """Embed artist/title (and lyrics) in a freshly downloaded file.

    The artist is the creator declared for this specific clip. Playlist
    owners are never used as an artist fallback. For other tracks the source
    handle remains the fallback when no producer was resolved. A tagging
    failure is logged and counted, never fatal: the audio is on disk and
    playable, which matters far more than its metadata.

    If ``lyrics_sidecar`` is "lrc" or "txt", also write a sidecar lyrics file.
    """
    artist = "Unknown artist" if track.artist_is_fallback else track.artist or "Unknown artist"
    result = tag_file(path, artist=artist, title=track.title, lyrics=track.lyrics)
    if result.tagged:
        stats["tagged"] = stats.get("tagged", 0) + 1
        logger.debug(
            "Tagged %s (%s): %s",
            _display_text(path.name),
            result.backend,
            _display_text(track.title),
        )
    else:
        logger.warning(
            "Could not tag %s: %s", _display_text(path.name), _display_text(result.reason)
        )

    # Write sidecar lyrics file if requested and lyrics are available
    if lyrics_sidecar and track.lyrics and track.lyrics.strip():
        write_lyrics_sidecar(path, track.lyrics, lyrics_sidecar)


def _record_sidecar(
    track: TrackInfo,
    base: Path,
    output_dir: Path,
    handle: str,
    stats: dict[str, int],
    *,
    overwrite: bool,
    source: str,
    owned_names: set[str] | None = None,
    preserved_members: dict[Path, tuple[int, int, int, int]] | None = None,
) -> bool:
    """Write ``<base>.json`` recording the clip id and original title.

    Never fatal: a sidecar that cannot be written is logged and the audio on
    disk stays valid. ``overwrite=False`` (used for tracks that were already on
    disk) leaves any existing sidecar untouched.
    """
    target = sidecar_path(base)
    prior = None
    if overwrite and (target.exists() or target.is_symlink()):
        prior = None if target.is_symlink() else read_sidecar(target)
        if prior is None or str(prior["id"]).lower() != track.id.lower():
            logger.warning("Refusing to overwrite an unowned sidecar for %s", track.id)
            return False
    files = saved_files_for(base)
    if prior is not None and owned_names is not None:
        for name in prior["saved_files"]:
            if not isinstance(name, str) or Path(name).name != name:
                continue
            path = base.parent / name
            if preserved_members is None or path not in preserved_members or path.is_symlink():
                continue
            try:
                info = path.stat()
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == preserved_members[
                    path
                ]:
                    owned_names.add(name)
            except OSError:
                continue
    if owned_names is not None:
        files = [name for name in files if name in owned_names]
    if not files or not _is_contained(target, output_dir):
        return False
    first = base.parent / files[0]
    downloaded_at = datetime.fromtimestamp(first.stat().st_mtime, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data = build_sidecar(
        track, handle=handle, saved_files=files, downloaded_at=downloaded_at, source=source
    )
    if write_sidecar(target, data, overwrite=overwrite):
        stats["sidecars"] = stats.get("sidecars", 0) + 1
        logger.debug("Wrote sidecar %s", _display_text(target.name))
        return True
    return False


async def _download_single(
    track: TrackInfo,
    out_base: Path,
    output_dir: Path,
    fetcher: Fetcher,
    semaphore: asyncio.Semaphore,
    stats: dict[str, int],
    bar: tqdm | None,
    preferred_format: str | None = None,
    audio_only: bool = False,
    extract_audio: bool = False,
    covers: bool = False,
    handle: str = "",
    legacy_base: Path | None = None,
    lyrics_sidecar: str | None = None,
    budget: DownloadBudget | None = None,
    sidecar: bool = False,
    keep_video: bool = False,
    pacer: PlaybackPacer | None = None,
    replace_existing: bool = False,
    producer_preflight_done: bool = False,
    known_existing: bool = False,
    sidecars_written: set[tuple[str, Path]] | None = None,
    published_media: dict[str, set[str]] | None = None,
    replacement_paths: dict[Path, tuple[int, int, int, int]] | None = None,
    preserved_members: dict[Path, tuple[int, int, int, int]] | None = None,
) -> Literal["downloaded", "skipped_exists", "skipped_capped", "failed"]:
    """Try each declared public media URL, atomically saving the first valid one.

    ``out_base`` is pre-assigned by the caller (uniqueness is resolved up front,
    single-threaded), so an existing file at ``out_base`` provably belongs to
    this track and means a completed prior run — safe to skip.

    With ``audio_only``, video-only tracks (their best candidate is an mp4
    video) are skipped without a network request — the bulk of an archival
    run's disk use. With ``covers``, the track's JPEG cover art is downloaded
    (and optionally recompressed) after a successful audio download.

    ``pacer`` (#161) gates the start of the transfer behind a playback-duration
    pacer; tracks that are skipped (already archived, capped, video-only) return
    before it, so they never wait and never issue a media GET.

    ``handle`` is the profile the track came from: it is written as the
    ``artist`` tag so Music Assistant credits the profile instead of showing
    ``[unknown]``. ``legacy_base`` is the pre-1.6 name for the same
    track — an archive that still has it is skipped, never re-fetched.
    ``budget`` is the rotate-off cap (#93): once it is spent, remaining tracks
    are recorded as ``skipped_capped`` instead of fetched.

    With ``sidecar``, a ``<stem>.json`` recording the clip id and original
    title is written beside each saved track; a track already on disk without
    one gets it backfilled (no network, never overwriting an existing sidecar).
    """
    async with semaphore:
        # Explicit opt-in: HA and existing CLI extraction never retain MP4.
        keep_video = keep_video and not audio_only and extract_audio
        # Both the current name and the pre-1.6 one for this track, so an
        # archive that still uses periods is not downloaded a second time.
        bases = (out_base, legacy_base) if legacy_base else (out_base,)
        for ext in _MEDIA_SUFFIXES:
            for base in bases:
                path = Path(str(base) + ext)
                if not replace_existing and path.exists() and path.stat().st_size > 1024:
                    if not known_existing:
                        stats["failed"] += 1
                        logger.warning("Refusing unowned existing media for %s", track.id)
                        if bar:
                            bar.update(1)
                        return "failed"
                    # Existing audio is not proof that its video was archived.
                    # Enrich only when the track actually advertises an MP4;
                    # ordinary reruns (including video+M4A pairs) stay offline.
                    advertised_video = any(
                        _suffix_for(url) == ".mp4" for url in _media_candidates(track)
                    )
                    if (
                        keep_video
                        and ext != ".mp4"
                        and advertised_video
                        and not any((Path(str(base) + ".mp4")).is_file() for base in bases)
                    ):
                        continue
                    stats["skipped_exists"] += 1
                    if sidecar:
                        _record_sidecar(
                            track,
                            base,
                            output_dir,
                            handle,
                            stats,
                            overwrite=False,
                            source="existing",
                        )
                    if bar:
                        bar.update(1)
                    return "skipped_exists"

        # An existing audio track gaining its MP4 must not consume a new track slot.
        existing_track = (
            keep_video
            and known_existing
            and any(
                path.is_file() and path.stat().st_size > 1024
                for base in bases
                for ext in _MEDIA_SUFFIXES
                for path in (Path(str(base) + ext),)
            )
        )
        if budget is not None and not budget.allows(existing_track=existing_track):
            stats["skipped_capped"] += 1
            logger.debug(
                "Cap reached; not downloading %s (%s)",
                _display_text(track.title),
                _display_text(track.id),
            )
            if bar:
                bar.update(1)
            return "skipped_capped"

        candidates = _media_candidates(track, preferred_format)
        if not candidates or (
            track.from_playlist and not track.artist and not producer_preflight_done
        ):
            try:
                resolved = await _resolve_media_urls(
                    track,
                    fetcher,
                    resolve_artist=track.from_playlist
                    and not track.artist
                    and not producer_preflight_done,
                )
                candidates = list(dict.fromkeys([*resolved, *candidates]))
            except ResolutionError as exc:
                logger.debug(
                    "Resolution failed for %s at %s (%s)",
                    track.id,
                    _display_text(redact_url(track.page_url)),
                    type(exc).__name__,
                )

        # Split candidates into audio (m4a, mp3, wav) and video (mp4)
        def _is_mp4(url: str) -> bool:
            return url.lower().split("?", 1)[0].endswith(".mp4")

        audio_candidates = [u for u in candidates if not _is_mp4(u)]
        video_candidates = [u for u in candidates if _is_mp4(u)]

        # #161: pace only real transfers, and only once per track — every
        # candidate retry for the same track belongs to the same slot.
        if pacer is not None and candidates:
            await pacer.wait(track.duration_sec)

        if audio_only:
            # Try audio candidates first
            tried_audio = False
            for media_url in audio_candidates:
                tried_audio = True
                result = await _try_download_candidate(
                    media_url,
                    track,
                    out_base,
                    output_dir,
                    fetcher,
                    stats,
                    bar,
                    handle,
                    covers,
                    lyrics_sidecar=lyrics_sidecar,
                    budget=budget,
                    sidecar=sidecar,
                    replace_existing=replace_existing,
                    sidecars_written=sidecars_written,
                    published_media=published_media,
                    replacement_paths=replacement_paths,
                    preserved_members=preserved_members,
                )
                if result == "downloaded":
                    return "downloaded"

            # All audio candidates failed; fall back to video if extract_audio is enabled
            if tried_audio and extract_audio and video_candidates:
                logger.debug(
                    "Audio candidates failed for %s; falling back to mp4 with extraction",
                    track.id,
                )
                for media_url in video_candidates:
                    result = await _try_download_candidate(
                        media_url,
                        track,
                        out_base,
                        output_dir,
                        fetcher,
                        stats,
                        bar,
                        handle,
                        covers,
                        extract_audio=True,
                        lyrics_sidecar=lyrics_sidecar,
                        budget=budget,
                        sidecar=sidecar,
                        replace_existing=replace_existing,
                        sidecars_written=sidecars_written,
                        published_media=published_media,
                        replacement_paths=replacement_paths,
                        preserved_members=preserved_members,
                    )
                    if result == "downloaded":
                        return "downloaded"

            # No audio candidates worked, and either no video fallback or extraction not enabled
            if candidates and not audio_candidates:
                stats["skipped_video"] += 1
                logger.debug("Skipping video-only track %s (--audio-only)", track.id)
            elif tried_audio:
                stats["failed"] += 1
                logger.warning("No accessible public media for %s", track.id)
            else:
                stats["failed"] += 1
                logger.warning("No accessible public media for %s", track.id)
            if bar:
                bar.update(1)
            return "failed"

        # Video+audio mode prefers the MP4 so the M4A can be remuxed from
        # that same source, rather than stopping after the first audio URL.
        # Plain --videos without --extract-audio retains historical ordering.
        if keep_video:
            candidates.sort(key=lambda url: _suffix_for(url) != ".mp4")
        existing_audio = keep_video and any(
            path.is_file() and path.stat().st_size > 1024
            for base in bases
            for ext in _MEDIA_SUFFIXES
            if ext != ".mp4"
            for path in (Path(str(base) + ext),)
        )
        for media_url in candidates:
            if existing_audio and _suffix_for(media_url) != ".mp4":
                # Video enrichment failed or was unavailable. Do not clobber a
                # previously tagged M4A/MP3/WAV merely to retry the audio path.
                stats["skipped_exists"] += 1
                if bar:
                    bar.update(1)
                return "skipped_exists"
            result = await _try_download_candidate(
                media_url,
                track,
                out_base,
                output_dir,
                fetcher,
                stats,
                bar,
                handle,
                covers,
                extract_audio=extract_audio,
                keep_video=keep_video,
                existing_track=existing_track,
                lyrics_sidecar=lyrics_sidecar,
                budget=budget,
                sidecar=sidecar,
                replace_existing=replace_existing,
                sidecars_written=sidecars_written,
                published_media=published_media,
                replacement_paths=replacement_paths,
                preserved_members=preserved_members,
            )
            if result == "downloaded":
                return "downloaded"
            if result == "skipped_capped":
                if bar:
                    bar.update(1)
                return "skipped_capped"

        stats["failed"] += 1
        logger.warning("No accessible public media for %s", track.id)
        if bar:
            bar.update(1)
        return "failed"


def resolve_output_dir(output_dir: Path, subdir: str | None) -> Path:
    """Resolve the final output directory.

    ``subdir`` nests output under ``output_dir/<subdir>`` (a sanitized
    single component) so per-source archives stay separated; ``None``
    writes directly into ``output_dir``. Does not create the directory.
    """
    resolved = output_dir.expanduser().resolve()
    if subdir:
        resolved = resolved / sanitize_component(subdir)
    return resolved


def extract_audio_files(paths: list[Path]) -> list[Path]:
    """Remux video-MP4 tracks to audio-only M4A losslessly via ffmpeg.

    ``-c:a copy`` never re-encodes: the AAC stream is byte-identical to the
    source. Files that are already audio (m4a/mp3/wav) or missing/failed
    are returned unchanged input entries are skipped; only newly written
    outputs are returned. Returns [] when ffmpeg is unavailable.
    """
    if any(
        src.is_file()
        and src.suffix.lower() == ".mp4"
        and not src.with_suffix(".m4a").exists()
        and not src.with_suffix(".m4a").is_symlink()
        for src in paths
    ):
        require_embedded_tag_writes()
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning("--extract-audio requested but ffmpeg not found on PATH; skipping")
        return []
    written: list[Path] = []
    for src in paths:
        if not src.is_file() or src.suffix.lower() != ".mp4":
            continue
        dest = src.with_suffix(".m4a")
        if _remux_without_overwrite(src, dest, ffmpeg):
            written.append(dest)
    return written


def _new_download_stats(total: int) -> dict[str, int]:
    """Build the same statistics for normal and genuinely empty runs."""
    return {
        "total": total,
        "dry_run": 0,
        "downloaded": 0,
        "skipped_exists": 0,
        "skipped_duplicate": 0,
        "skipped_capped": 0,
        "skipped_rotated": 0,
        "failed": 0,
        "skipped_video": 0,
        "skipped_blocked": 0,
        "rotated_tracks": 0,
        "rotated_files": 0,
        "rotated_bytes": 0,
        "tagged": 0,
        "sidecars": 0,
        "replaced": 0,
        "invalid_existing": 0,
        "metadata_updated": 0,
        "metadata_unchanged": 0,
        "metadata_missing": 0,
        "metadata_failed": 0,
        "metadata_unresolved": 0,
    }


def _preflight_download_before_lock(func):
    """Reject disabled work before creating an archive or its lockfile."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def guarded(*args, **kwargs):
        values = signature.bind(*args, **kwargs)
        values.apply_defaults()
        if (
            not values.arguments["tracks"]
            and not values.arguments["rotate"]
            and not values.arguments["source"]
            and not values.arguments["max_tracks"]
            and not values.arguments["max_disk_mb"]
        ):
            root = resolve_output_dir(Path(values.arguments["output_dir"]), None)
            if root.exists() or root.is_symlink():
                # No archive writes occur in this branch. Still refuse a
                # journal that is already present at this read-only check.
                from .producer_migration import raise_if_migration_pending

                raise_if_migration_pending(root)
            return _new_download_stats(0)
        if not values.arguments["dry_run"]:
            raise_if_unsupported_archive_write()
        _require_trusted_framing()
        require_audio_sync(**values.arguments)
        return await func(*args, **kwargs)

    return guarded


async def _download_tracks_legacy(
    tracks: list[TrackInfo],
    fetcher: Fetcher,
    concurrency: int,
    output_dir: Path,
    pattern: str,
    handle: str,
    dry_run: bool = False,
    preferred_format: str | None = None,
    audio_only: bool = False,
    covers: bool = False,
    subdir: str | None = None,
    extract_audio: bool = False,
    blocked_ids: frozenset[str] = frozenset(),
    lyrics_sidecar: str | None = None,
    min_free_mb: int = 2048,
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    rotate: bool = False,
    sidecar: bool = True,
    keep_video: bool = False,
    playback_paced: bool = False,
    pacer: PlaybackPacer | None = None,
    metadata_only: bool = False,
    update_existing: bool = False,
    force_redownload: bool = False,
    library: LibraryIndex | None = None,
    source: str | None = None,
) -> dict[str, int]:
    """Download all tracks, bounded by concurrency; returns statistics.

    Nonempty runs, including dry runs and no-op-only lists, are temporarily
    blocked because producer provenance is not verified for every input route.
    Empty-list extraction or rotation is not blocked by the attribution guard.
    The archive-lock wrapper may create its lockfile before this check.

    ``max_tracks`` / ``max_disk_mb`` cap the *archive* (the whole output tree,
    not just this source's subfolder); both are 0 = off by default (#93).
    With ``rotate`` off a cap stops new downloads at the limit and nothing is
    ever deleted; with ``rotate`` on the oldest tracks are pruned after the
    downloads finish, until the archive fits both caps again.

    With ``sidecar`` (default) each saved track gets a ``<stem>.json`` recording
    its clip id and original title, and a track whose id an existing sidecar
    already records is skipped even if its filename differs.

    ``playback_paced`` (#161) spaces real media transfers by the previous
    track's playback duration. Pass a shared ``pacer`` to keep that spacing
    across several sources in one bulk run; skipped, capped and dry-run tracks
    never wait. The library default stays off so callers that do not ask for
    pacing keep their previous timing; the CLI enables it by default.

    ``library`` (#158) is the global clip-UUID index; without one the archive
    root is scanned. A clip recorded in *any* subfolder or legacy profile
    folder counts as archived, so two playlists share one transfer and one
    physical file. ``source`` names the playlist/profile whose ordered
    membership is recorded beside the archive (never the audio itself).

    ``metadata_only`` (#159) reconciles tags/sidecars for already-owned clips
    and never issues a media request. ``update_existing`` does the same repair
    during an ordinary sync. ``force_redownload`` (#167) is the only way to
    replace existing audio: it probes the current recording read-only, streams
    the replacement to ``.part`` and swaps it in only after it validates.
    """
    # TrackInfo does not carry independently verified producer provenance.
    # Stop before artist-derived filename previews, tagging or sidecar writes;
    # discovery remains available until structured Flight proof is implemented.
    if tracks:
        _require_trusted_framing()
        if not dry_run:
            require_embedded_tag_writes()
    if extract_audio and not dry_run and not metadata_only:
        require_embedded_tag_writes()
    # Keep-video is meaningful only for explicit video+audio extraction.
    # This also protects direct API callers outside the CLI guard.
    keep_video = keep_video and not audio_only and extract_audio
    if pacer is None and playback_paced:
        pacer = PlaybackPacer()
    archive_root = resolve_output_dir(output_dir, None)
    output_dir = resolve_output_dir(output_dir, subdir)
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    capped = max_tracks > 0 or max_disk_mb > 0
    state = RotationState.load(archive_root) if capped else None
    if state is not None and not dry_run:
        # Reconcile any rotation interrupted mid-prune before deciding what is
        # already archived, so a half-deleted group is never suppressed.
        recover_rotation(archive_root, state)

    stats = _new_download_stats(len(tracks))

    # Blocklist first: a blocked track is skipped before any request, in
    # every mode (dry-run included) — deliberately not fetching is the point.
    active: list[TrackInfo] = []
    for track in tracks:
        if track.id.lower() in blocked_ids:
            stats["skipped_blocked"] += 1
            print(
                "Skipping blocked track (see .blocked): "
                f"{_display_text(track.title)} [{_display_text(track.id)}]"
            )
        else:
            active.append(track)
    tracks = active

    # Tracks a previous rotation deleted must not be fetched again (#93):
    # re-downloading them would defeat the cap the moment the next sync runs.
    if state is not None and state.pruned:
        pruned_ids = {clip_id.lower() for clip_id in state.pruned}
        kept = [track for track in tracks if track.id.lower() not in pruned_ids]
        stats["skipped_rotated"] = len(tracks) - len(kept)
        if stats["skipped_rotated"]:
            logger.info(
                "Skipping %d track(s) pruned by an earlier rotation (not re-downloading)",
                stats["skipped_rotated"],
            )
            tracks = kept

    # Global UUID index (#158): a recording archived under any subfolder —
    # including the legacy per-profile folders — is reused, never duplicated.
    index = library if library is not None else LibraryIndex(archive_root)
    # Read existing identities even if this run will not write sidecars.
    index.scan()

    # Every route (including previews and prefilled direct API inputs) must
    # establish producer identity and candidate URLs from its own clip page.
    tracks = await verify_track_producers(tracks, fetcher)

    if metadata_only:
        # #159: repair existing recordings only. This branch is deliberately
        # before every media/download path, so --metadata-only cannot fetch.
        reconcile(
            tracks,
            index,
            handle=handle,
            dry_run=dry_run,
            sidecar=sidecar,
            lyrics_sidecar=lyrics_sidecar,
            stats=stats,
        )
        stats["dry_run"] = len(tracks) if dry_run else 0
        return stats

    if dry_run:
        stats["dry_run"] = len(tracks)
        for track in tracks:
            if force_redownload:
                entry = index.get(track.id) if sidecar else None
                if entry is not None:
                    for media in entry.media:
                        reason = probe_media(media)
                        print(
                            f"[dry-run] probe {_display_text(media.name)}: "
                            f"{_display_text(reason or 'ok')}"
                        )
            fname = build_filename(
                pattern,
                title=track.title,
                handle=handle,
                clip_id=track.id,
                artist=None if track.artist_is_fallback else track.artist,
            )
            suffix = ""
            if (
                audio_only
                and (track.audio_url or "").lower().split("?", 1)[0].endswith(".mp4")
                and not track.media_urls
            ):
                suffix = " (video-only; skipped with --audio-only)"
            print(
                f"[dry-run] {_display_text(output_dir / fname)}{suffix} "
                f"<- {_display_text(track.title)}"
            )
            if covers:
                print(
                    f"[dry-run] {_display_text(output_dir / (fname + '.cover.jpg'))} "
                    f"<- {_display_text(track.title)}"
                )
        if rotate and capped:
            stats.update(
                rotate_archive(
                    archive_root,
                    max_tracks=max_tracks,
                    max_disk_mb=max_disk_mb,
                    dry_run=True,
                    state=state,
                )
            )
        return stats

    # Rotate off: the caps stop *downloading* at the limit (never delete).
    # Rotate on: download freely, oldest-first pruning happens afterwards.
    budget: DownloadBudget | None = None
    if capped and not rotate:
        existing_tracks, existing_bytes = archive_usage(archive_root)
        budget = DownloadBudget(
            tracks_left=(max(0, max_tracks - existing_tracks) if max_tracks else None),
            bytes_left=(max(0, max_disk_mb * MEGABYTE - existing_bytes) if max_disk_mb else None),
        )
        if not budget.allows():
            logger.info(
                "Archive already at its cap (%d track(s), %d bytes); not downloading "
                "(rotate is off, so nothing is deleted)",
                existing_tracks,
                existing_bytes,
            )

    # With paired output and any cap, serialize reservations so concurrent
    # tasks cannot spend the same byte or track-count allowance.
    pair_budget = keep_video and budget is not None
    semaphore = asyncio.Semaphore(1 if pair_budget else concurrency)
    tasks: list[asyncio.Task[str]] = []

    # Pre-assign unique output bases single-threaded so concurrent tasks can
    # never race for the same filename, and so an existing file is always
    # attributable to the track it was assigned to.
    assigned: set[Path] = set()
    assigned_legacy: set[Path] = set()
    scheduled_ids: set[str] = set()
    bases: list[Path] = []
    # Tracks that actually got a task (an id the archive already has is skipped).
    scheduled: list[TrackInfo] = []
    intent_paths: list[Path | None] = []
    sidecars_written: set[tuple[str, Path]] = set()
    published_media: dict[str, set[str]] = {}
    try:
        with tqdm(total=len(tracks), unit="track", desc="Downloading") as bar:
            for track in tracks:
                if track.id.lower() in scheduled_ids:
                    stats["skipped_duplicate"] += 1
                    bar.update(1)
                    continue
                scheduled_ids.add(track.id.lower())
                _check_disk_space(output_dir, min_free_mb)
                entry = index.get(track.id)
                if track.id.lower() in index.conflicts:
                    stats["failed"] += 1
                    logger.warning("Refusing to download conflicting archive ID %s", track.id)
                    bar.update(1)
                    continue
                if entry is None:
                    try:
                        if prepare_intent(archive_root, track.id):
                            if force_redownload:
                                raise UnsafeDownloadIntent(
                                    "force redownload of a sidecar-free track requires review"
                                )
                            stats["skipped_exists"] += 1
                            bar.update(1)
                            continue
                    except UnsafeDownloadIntent as exc:
                        stats["failed"] += 1
                        logger.warning("Refusing interrupted clip %s: %s", track.id, exc)
                        bar.update(1)
                        continue
                producer_preflight_done = False
                if (
                    (
                        pattern_uses_artist(pattern)
                        or track.from_playlist
                        or track.artist_is_fallback
                    )
                    and entry is None
                    and (budget is None or budget.allows())
                    and (not track.artist or track.artist_is_fallback)
                ):
                    producer_preflight_done = True
                    try:
                        resolved = await _resolve_media_urls(track, fetcher, resolve_artist=True)
                        track.media_urls = list(dict.fromkeys([*resolved, *track.media_urls]))
                    except ResolutionError:
                        logger.info("Could not resolve producer for %s", track.id)
                    if not track.artist or track.artist_is_fallback:
                        track.artist = None
                        track.artist_is_fallback = True
                base = output_dir / build_filename(
                    pattern, title=track.title, handle=handle, clip_id=track.id, artist=track.artist
                )
                # Pre-1.6 name for the same track: an archive that still carries it
                # is recognised as already-downloaded and skipped — the
                # migration is a retag, never a re-download. Deduped on its own
                # set so two same-titled tracks cannot make each other look
                # "already on disk" through a shared legacy name.
                legacy = _unique_base(
                    output_dir
                    / build_filename(
                        pattern,
                        title=track.title,
                        handle=handle,
                        clip_id=track.id,
                        artist=track.artist,
                        ma_safe=False,
                    ),
                    assigned_legacy,
                )
                assigned_legacy.add(legacy)
                target_dir = output_dir
                replace_existing = False
                archived_base: Path | None = entry.base if entry is not None else None
                if force_redownload and entry is not None:
                    # #167: replace the recording where it actually lives, not a
                    # second copy in this source's folder.
                    target_dir = entry.directory
                    base = entry.base
                    legacy = entry.base
                    replace_existing = True
                    for media in entry.media:
                        reason = probe_media(media)
                        if reason:
                            stats["invalid_existing"] += 1
                            logger.warning(
                                "Existing recording fails integrity probe (%s): %s",
                                reason,
                                media.name,
                            )
                else:
                    if pattern_uses_artist(pattern) and entry is None:
                        chosen = _producer_unique_base(
                            base, assigned, track.id, index, archive_root
                        )
                        if chosen is None:
                            stats["failed"] += 1
                            logger.warning("Refusing occupied producer filename for %s", track.id)
                            bar.update(1)
                            continue
                        base = chosen
                        legacy = base
                    else:
                        base = _unique_base(base, assigned)
                    # A filename is not proof of ownership, even for an explicit
                    # replacement. Only the verified indexed entry above may be replaced.
                    replace_existing = False
                    if keep_video and archived_base is not None and entry is not None:
                        if entry.directory == output_dir.resolve():
                            # Clip ID wins over an updated title, sanitisation or
                            # filename template. Reuse the existing media/sidecar,
                            # never make a pair under a second name.
                            base = archived_base
                            legacy = archived_base
                        else:
                            archived_base = None
                assigned.add(base)
                advertised_video = any(
                    _suffix_for(url) == ".mp4" for url in _media_candidates(track)
                )
                existing_mp4 = (Path(str(base) + ".mp4")).is_file() or (
                    Path(str(legacy) + ".mp4")
                ).is_file()
                enrich_video = keep_video and advertised_video and not existing_mp4
                if entry is not None and not replace_existing and not enrich_video:
                    # The name is still reserved above so a same-titled clip cannot
                    # be mistaken for this one; only the download is skipped.
                    stats["skipped_exists"] += 1
                    if update_existing:
                        reconcile(
                            [track],
                            index,
                            handle=handle,
                            dry_run=dry_run,
                            sidecar=sidecar,
                            lyrics_sidecar=lyrics_sidecar,
                            stats=stats,
                        )
                    bar.update(1)
                    continue
                marker: Path | None = None
                if entry is None:
                    try:
                        marker = reserve_intent(archive_root, track.id, base)
                    except UnsafeDownloadIntent as exc:
                        stats["failed"] += 1
                        logger.warning("Could not reserve clip %s: %s", track.id, exc)
                        bar.update(1)
                        continue
                bases.append(base)
                scheduled.append(track)
                intent_paths.append(marker)
                tasks.append(
                    asyncio.create_task(
                        _download_single(
                            track,
                            base,
                            target_dir,
                            fetcher,
                            semaphore,
                            stats,
                            bar,
                            preferred_format,
                            audio_only,
                            extract_audio,
                            covers,
                            handle,
                            None if legacy == base else legacy,
                            lyrics_sidecar,
                            budget,
                            sidecar,
                            keep_video,
                            pacer,
                            replace_existing,
                            producer_preflight_done,
                            bool(
                                entry is not None
                                and entry.base == base
                                and entry.directory == target_dir.resolve()
                            ),
                            sidecars_written,
                            published_media,
                            dict(entry.media_identities)
                            if replace_existing and entry is not None
                            else None,
                            dict(entry.member_identities) if entry is not None else None,
                        )
                    )
                )
            # Shield the group so repeated cancellation of the parent does not
            # repeatedly cancel a child's cleanup while the archive lock is held.
            work = asyncio.gather(*tasks)
            work.add_done_callback(
                lambda future: None if future.cancelled() else future.exception()
            )
            outcomes = await asyncio.shield(work)
            fresh_index = LibraryIndex(archive_root).scan() if any(intent_paths) else None
            for track, base, marker, outcome in zip(
                scheduled, bases, intent_paths, outcomes, strict=True
            ):
                if marker is None or outcome != "downloaded" or fresh_index is None:
                    continue
                if not sidecar:
                    sync_directory(base.parent)
                    complete_without_sidecar(
                        marker, base, published_media.get(track.id.lower(), set())
                    )
                    continue
                side = sidecar_path(base)
                recorded = fresh_index.get(track.id)
                if (
                    (track.id.lower(), side) in sidecars_written
                    and recorded is not None
                    and recorded.sidecar == side
                    and recorded.base == base
                ):
                    sync_directory(side.parent)
                    marker.unlink(missing_ok=True)
                    sync_directory(marker.parent)
    finally:
        # Preserve an existing failure if cancellation arrives during cleanup.
        active_error = sys.exception()
        # Never release the archive lock while spawned writers are alive.
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            cancelled_during_cleanup = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Do not release the lock or cancel finalizers again.
                    cancelled_during_cleanup = True
            if cancelled_during_cleanup and active_error is None:
                raise asyncio.CancelledError

    # Rotation ages live by stem: remember what was just downloaded so the
    # next run can order by Suno's clock rather than mtime.
    if state is not None:
        for track, base, outcome in zip(scheduled, bases, outcomes, strict=True):
            if outcome == "downloaded":
                state.record(archive_root, base, clip_id=track.id, created_at=track.created_at)
        state.save(archive_root)

    if extract_audio and not dry_run:
        # A sidecar-free reservation fingerprints the exact published media.
        # Bulk extraction would change that set without a journal; leave those
        # MP4s untouched until a recoverable manifest update is supported.
        unprocessed = [
            path
            for path in sorted(output_dir.glob("*.mp4"))
            if not has_reserved_base(archive_root, path.with_suffix(""))
        ]
        if keep_video and budget is not None and budget.bytes_left is not None:
            selected: list[Path] = []
            reserved = 0
            for src in unprocessed:
                if src.with_suffix(".m4a").exists():
                    continue
                allowance = src.stat().st_size + _PAIR_OVERHEAD_BYTES
                if reserved + allowance <= budget.bytes_left:
                    selected.append(src)
                    reserved += allowance
                else:
                    logger.info(
                        "Skipping existing MP4 audio remux at byte cap: %s", _display_text(src.name)
                    )
            unprocessed = selected
        written = extract_audio_files(unprocessed)
        if keep_video and budget is not None and budget.bytes_left is not None:
            # This remux is for an already-counted archived track, not a
            # second track; debit bytes only, not tracks_left.
            budget.bytes_left = max(0, budget.bytes_left - sum(p.stat().st_size for p in written))
        if written:
            logger.info("Extracted %d M4A file(s) via ffmpeg", len(written))
        if keep_video and written:
            # These may be pre-existing MP4s skipped by resume/sidecar checks.
            # Retag the new M4As and update sidecar file inventories.
            by_stem: dict[str, tuple[TrackInfo, Path]] = {}
            for track, base in zip(scheduled, bases, strict=True):
                by_stem[base.name] = (track, base)
            for track in tracks:
                prior = index.get(track.id)
                if prior is not None:
                    by_stem.setdefault(prior.base.name, (track, prior.base))
                for safe in (True, False):
                    name = build_filename(
                        pattern,
                        title=track.title,
                        handle=handle,
                        clip_id=track.id,
                        artist=track.artist,
                        ma_safe=safe,
                    )
                    by_stem.setdefault(name, (track, output_dir / name))
            for m4a in written:
                found = by_stem.get(m4a.stem)
                if found is None:
                    continue
                track, base = found
                _write_tags(m4a, track, handle, stats, lyrics_sidecar)
                if sidecar:
                    _record_sidecar(
                        track,
                        base,
                        output_dir,
                        handle,
                        stats,
                        overwrite=True,
                        source="existing",
                    )

    if rotate and capped:
        stats.update(
            rotate_archive(
                archive_root,
                max_tracks=max_tracks,
                max_disk_mb=max_disk_mb,
                dry_run=False,
                state=state,
            )
        )

    # #158: membership is a set of ordered UUID references, independent of
    # whether this run transferred anything. Written atomically; a repeated
    # sync with the same ids does not rewrite the file.
    if source:
        record_membership(archive_root, source, [track.id for track in tracks])

    return stats


async def _execute_downloads(**options):
    from .linux_downloads import download_additions

    return await download_additions(**options)


@_preflight_download_before_lock
@archive_locked_if_capped
async def download_tracks(
    tracks: list[TrackInfo],
    fetcher: Fetcher,
    concurrency: int,
    output_dir: Path,
    pattern: str,
    handle: str,
    dry_run: bool = False,
    preferred_format: str | None = None,
    audio_only: bool = True,
    covers: bool = True,
    subdir: str | None = None,
    extract_audio: bool = True,
    blocked_ids: frozenset[str] = frozenset(),
    lyrics_sidecar: str | None = None,
    min_free_mb: int = 2048,
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    rotate: bool = False,
    sidecar: bool = True,
    keep_video: bool = False,
    playback_paced: bool = False,
    pacer: PlaybackPacer | None = None,
    metadata_only: bool = False,
    update_existing: bool = False,
    force_redownload: bool = False,
    library: LibraryIndex | None = None,
    source: str | None = None,
) -> dict[str, int]:
    """Public CLI/API additions: audio and cover images by default."""
    return await _execute_downloads(**locals())
