"""Additions-only Linux download engine shared by CLI, dashboard and API.

Indexed recordings are reused unchanged. New media, tags, sidecar and requested
adjuncts are staged as one group and published through a durable UUID intent.
Indexed repairs retain the separate offline migration/repair safety gates.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .download_failures import ARCHIVED_PREFIX, report_failed_track
from .fetcher import Fetcher
from .library import LibraryIndex, record_membership
from .lossless_mp4 import cover_data_type, write_isolated_mp4
from .media_process import run as run_media_process
from .models import TrackInfo
from .naming import build_filename
from .pacing import PlaybackPacer
from .producer_lookup import ProducerLookupError, verify_track_producers
from .publication import file_hash, publish_prepared, sync_directory
from .sidecar import _load_identity_json
from .sync_capacity import report_capacity
from .tagging import TaggingUnavailable, write_lyrics_sidecar
from .urls import UUID_RE, is_allowed_media_url
from .verified_sync import (
    MAX_MEDIA,
    directory,
    durable_json,
    packet_signature,
    prepare_audio,
    require_owned_library,
)

log = logging.getLogger(__name__)


def require_audio_sync(**options: Any) -> None:
    """Reject unsupported operations before discovery, locks or archive writes."""
    if "tracks" in options and any(
        not isinstance(track.id, str) or not UUID_RE.fullmatch(track.id)
        for track in options["tracks"]
    ):
        raise ProducerLookupError("invalid clip ID")
    if "output_dir" in options:
        selected_root = Path(options["output_dir"]).expanduser().absolute()
        if any(path.is_symlink() for path in (selected_root, *selected_root.parents)):
            raise ValueError("Archive root must not cross a symlink")
    unsupported = [
        name
        for name in ("metadata_only", "update_existing", "force_redownload", "rotate", "keep_video")
        if options.get(name)
    ]
    if options.get("sidecar", True) is False:
        unsupported.append("sidecar-free output")
    if options.get("preferred_format", options.get("fmt")) not in (None, "m4a"):
        unsupported.append("non-M4A output")
    if unsupported:
        raise TaggingUnavailable(
            "These options require separately reviewed publication support: "
            + ", ".join(unsupported)
            + "; use the offline producer migration for indexed repairs"
        )
    if not sys.platform.startswith("linux"):
        raise TaggingUnavailable("This download engine currently targets Linux")
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "link")):
        raise TaggingUnavailable("Required Linux publication operations are unavailable")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise TaggingUnavailable("FFmpeg and ffprobe are required before downloading")
    if importlib.util.find_spec("mutagen") is None:
        raise TaggingUnavailable("Mutagen is required to verify prepared tags")


def _pending_state(root: Path) -> None:
    metadata = root / ".library"
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
        raise ValueError("Unsafe archive metadata directory")
    for name in ("tag-operation-intent.json",):
        path = metadata / name
        if path.exists() or path.is_symlink():
            raise ValueError("Pending indexed mutation requires reviewed recovery")
    for name in ("download-intents", "tag-intents"):
        path = metadata / name
        if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise ValueError("Pending legacy publication requires reviewed recovery")


def _pruned_ids(root: Path) -> frozenset[str]:
    """Honor validated legacy tombstones without rewriting rotation state."""
    journal = root / ".suno-rotation.journal"
    if journal.exists() or journal.is_symlink():
        raise ValueError("Pending rotation journal requires reviewed recovery")
    path = root / ".suno-rotation.json"
    if not path.exists() and not path.is_symlink():
        return frozenset()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 * 1024:
        raise ValueError("Unsafe legacy rotation state")
    data = _load_identity_json(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("tracks"), dict)
        or not isinstance(data.get("pruned"), list)
        or any(not isinstance(x, str) or not UUID_RE.fullmatch(x) for x in data["pruned"])
    ):
        raise ValueError("Legacy rotation state requires review")
    return frozenset(x.lower() for x in data["pruned"])


async def _stage_cover(track: TrackInfo, base: Path, output: Path, fetcher: Fetcher) -> Path | None:
    """Bounded private adjunct download; backpressure stops the entire run."""
    url = track.cover_url
    if not url or not is_allowed_media_url(url):
        return None
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".mkv"}:
        return None
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    path = Path(str(base) + ".cover" + suffix)
    try:
        await fetcher.rate_limiter.acquire(url)
        async with fetcher.client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code == 429:
                raise RuntimeError("Cover server requested backpressure; stop this run")
            if response.status_code != 200:
                return None
            size = 0
            with path.open("xb") as stream:
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise ValueError("Cover exceeds the transfer bound")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if size:
            image_type = cover_data_type(path.read_bytes())
            probe = run_media_process(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            streams = json.loads(probe.stdout).get("streams", [])
            expected = "mjpeg" if image_type == 13 else "png"
            if (
                len(streams) != 1
                or streams[0].get("codec_name") != expected
                or not 0 < streams[0].get("width", 0) * streams[0].get("height", 0) <= 40_000_000
            ):
                raise ValueError("Unsupported cover image")
            run_media_process(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            canonical = Path(str(base) + (".cover.jpg" if image_type == 13 else ".cover.png"))
            if path != canonical:
                path.rename(canonical)
            return canonical
    except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError):
        log.warning("Cover image unavailable or invalid for clip %s; keeping audio", track.id)
    path.unlink(missing_ok=True)
    return None


async def _video_cover(
    track: TrackInfo,
    source: Path,
    stage: Path,
    fetcher: Fetcher,
    pacer: PlaybackPacer | None,
    *,
    already_video: bool,
) -> Path | None:
    """Extract one still, reusing the audio source video whenever possible."""
    private_video = stage / "cover-source.part"
    image = stage / "adjunct.cover.jpg"
    try:
        if not already_video:
            video = next((u for u in track.media_urls if urlsplit(u).path.endswith(".mp4")), None)
            if video is None:
                return None
            if pacer is not None:
                await pacer.wait(track.duration_sec)
            await fetcher.rate_limiter.acquire(video)
            async with fetcher.client.stream("GET", video, follow_redirects=False) as response:
                if response.status_code == 429:
                    raise RuntimeError("Cover source requested backpressure; stop this run")
                if response.status_code != 200:
                    return None
                count = 0
                with private_video.open("xb") as stream:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        count += len(chunk)
                        if count > MAX_MEDIA:
                            raise ValueError("Video cover source exceeds transfer bound")
                        stream.write(chunk)
            source = private_video
        run_media_process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "thumbnail=30,scale=1000:-2",
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(image),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        cover_data_type(image.read_bytes())
        log.info("Created cover image from published video; temporary video will be discarded")
        return image
    except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError):
        image.unlink(missing_ok=True)
        log.warning("No usable video still for clip %s", track.id)
        return None
    finally:
        private_video.unlink(missing_ok=True)


def _new_base(root: Path, output: Path, track: TrackInfo, pattern: str, handle: str) -> Path:
    value = build_filename(
        pattern, title=track.title, artist=track.artist, handle=handle, clip_id=track.id
    )
    value = value.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(" ._")
    occupied = {path.name.casefold() for path in output.iterdir()} if output.exists() else set()
    suffixes = (
        ".m4a",
        ".mp3",
        ".mp4",
        ".wav",
        ".json",
        ".cover.jpg",
        ".cover.jpeg",
        ".cover.png",
        ".cover.webp",
        ".txt",
        ".lrc",
    )
    if any((value + suffix).casefold() in occupied for suffix in suffixes):
        value = value.encode("utf-8")[:140].decode("utf-8", "ignore") + "_" + track.id
    if any((value + suffix).casefold() in occupied for suffix in suffixes):
        raise ValueError("Unowned filename collision for " + track.id)
    result = output / value
    if not result.resolve().is_relative_to(root):
        raise ValueError("Download destination escapes archive")
    return result


async def download_additions(**options: Any) -> dict[str, int]:
    """Called under the public downloader's common lock, except read-only previews."""
    from .downloader import _new_download_stats
    from .producer_migration import raise_if_migration_pending
    from .rotate import archive_usage
    from .sidecar import build_sidecar, read_sidecar

    require_audio_sync(**options)
    tracks: list[TrackInfo] = options["tracks"]
    root = Path(options["output_dir"]).expanduser().resolve()
    dry = options["dry_run"]
    subdir = options.get("subdir")
    if subdir is not None and (
        not isinstance(subdir, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", subdir)
    ):
        raise ValueError("Source subfolder must be a single profile handle or source UUID")
    output = root / subdir if subdir else root
    if output.is_symlink():
        raise ValueError("Linked output directory")
    stats = _new_download_stats(len(tracks))
    blocked = options["blocked_ids"]
    active = []
    pruned = _pruned_ids(root) if root.exists() else frozenset()
    seen = set()
    for track in tracks:
        if track.id.lower() in blocked:
            stats["skipped_blocked"] += 1
        elif track.id.lower() in pruned:
            stats["skipped_rotated"] += 1
        elif track.id.lower() in seen:
            stats["skipped_duplicate"] += 1
        else:
            seen.add(track.id.lower())
            active.append(track)

    if root.exists():
        raise_if_migration_pending(root)
        _pending_state(root)
    intents = root / ".library" / "verified-sync-intents"
    if intents.is_symlink():
        raise ValueError("Linked publication intent directory")
    if intents.exists():
        if not intents.is_dir():
            raise ValueError("Invalid publication intent directory")
        markers = sorted(intents.iterdir())
        if dry and markers:
            raise ValueError("Pending publication must be recovered before preview")
        for marker in markers:
            if marker.suffix != ".json":
                raise ValueError("Unrecognized publication state")
            publish_prepared(root, marker)
    stages = root / ".suno-staging"
    if stages.is_symlink():
        raise ValueError("Linked staging directory")
    if stages.exists():
        if not stages.is_dir():
            raise ValueError("Invalid private staging directory")

        def failed_scan(exc):
            raise ValueError("Cannot inspect private staging") from exc

        for parent, dirs, files in os.walk(stages, followlinks=False, onerror=failed_scan):
            for name in dirs:
                path = Path(parent) / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError("Linked or special private staging directory")
            if files:
                raise ValueError("Unrecorded private staging requires investigation")
    index = LibraryIndex(root).scan()
    if index.conflicts:
        raise ValueError("Conflicting UUID ownership")
    if root.exists():
        require_owned_library(root, index)
    pending = []
    for track in active:
        if index.get(track.id) is not None:
            stats["skipped_exists"] += 1
            if not dry:
                print(ARCHIVED_PREFIX + track.id.lower(), flush=True)
        else:
            pending.append(track)
    report_capacity(root, pending, covers=options["covers"])
    # Existing identities are skipped before lookup or artist-derived naming.
    fetcher: Fetcher = options["fetcher"]
    verified = await verify_track_producers(pending, fetcher)
    handle, pattern = options["handle"], options["pattern"]
    if dry:
        stats["dry_run"] = len(verified)
        for track in verified:
            print(
                "[dry-run] "
                + str(_new_base(root, output, track, pattern, handle))
                + " <- "
                + track.title
            )
        return stats
    # Every mutation below belongs to a missing UUID. Nothing indexed is tagged,
    # extracted, replaced, enriched, rotated or deleted by this engine.
    if not verified:
        if options.get("source"):
            record_membership(root, options["source"], [t.id for t in active])
        return stats
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    sync_directory(output.parent)
    internals = directory(root / ".library")
    intents = directory(internals / "verified-sync-intents")
    stage_parent = directory(directory(root / ".suno-staging") / "linux-additions")
    pacer = options.get("pacer")
    if pacer is None and options["playback_paced"]:
        pacer = PlaybackPacer()
    if pacer is not None:
        pacer.bind_checkpoint(internals / "media-pacing.json")
    total_tracks, total_bytes = archive_usage(root)
    for queue_index, track in enumerate(verified):
        report_capacity(root, verified[queue_index:], covers=options["covers"])
        if (options["max_tracks"] and total_tracks >= options["max_tracks"]) or (
            options["max_disk_mb"] and total_bytes >= options["max_disk_mb"] * 1024 * 1024
        ):
            stats["skipped_capped"] += 1
            continue
        if shutil.disk_usage(root).free < options["min_free_mb"] * 1024 * 1024 + MAX_MEDIA * 2:
            raise ValueError("Library free-space reserve reached")
        base = _new_base(root, output, track, pattern, handle)
        stage = Path(tempfile.mkdtemp(prefix=track.id + "-", dir=stage_parent))
        incoming, audio = stage / "incoming.part", stage / "audio.part"
        retain = False
        try:
            ready = False
            used_video = False
            urls = track.media_urls
            if options["audio_only"] and not options["extract_audio"]:
                urls = [u for u in urls if not urlsplit(u).path.lower().endswith(".mp4")]
            rejected_statuses: set[int] = set()
            preparation_failed = False
            for url in urls:
                if pacer is not None:
                    await pacer.wait(track.duration_sec)
                if urlsplit(url).path.lower().endswith(".mp4"):
                    log.info("Using published video source for audio; video will be discarded")
                incoming.unlink(missing_ok=True)
                audio.unlink(missing_ok=True)
                try:
                    await fetcher.rate_limiter.acquire(url)
                    async with fetcher.client.stream(
                        "GET", url, follow_redirects=False
                    ) as response:
                        if response.status_code == 429:
                            raise RuntimeError("Media server requested backpressure; stop this run")
                        if response.status_code != 200:
                            rejected_statuses.add(response.status_code)
                            log.warning(
                                "Published media returned HTTP %s for clip %s",
                                response.status_code,
                                track.id,
                            )
                            continue
                        size = 0
                        with incoming.open("xb") as stream:
                            async for chunk in response.aiter_bytes(64 * 1024):
                                size += len(chunk)
                                if size > MAX_MEDIA:
                                    raise ValueError("Media exceeds the transfer bound")
                                stream.write(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                    prepare_audio(
                        incoming,
                        audio,
                        track.artist or "",
                        track.title,
                        allow_extraction=options["extract_audio"],
                    )
                    from mutagen.mp4 import MP4

                    tags = MP4(audio)
                    if tags.get("\xa9ART") != [track.artist] or tags.get("\xa9nam") != [
                        track.title
                    ]:
                        raise ValueError("Prepared artist/title verification failed")
                    used_video = urlsplit(url).path.lower().endswith(".mp4")
                    ready = True
                    break
                except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError) as exc:
                    preparation_failed = True
                    log.warning(
                        "Published media could not be prepared for clip %s (%s)",
                        track.id,
                        type(exc).__name__,
                    )
                    continue
            if not ready:
                reasons = []
                if rejected_statuses:
                    codes = ", ".join(f"HTTP {code}" for code in sorted(rejected_statuses))
                    reasons.append(f"Published media returned {codes}.")
                if preparation_failed:
                    reasons.append("A media source could not be downloaded or prepared.")
                if not urls:
                    reasons.append("Suno published no supported media source.")
                report_failed_track(track.id, track.title, " ".join(reasons))
                log.warning(
                    "Clip %s has no usable supported media source; left missing for retry", track.id
                )
                stats["failed"] += 1
                continue
            members = [(audio, Path(str(base) + ".m4a"))]
            if options["covers"]:
                cover = await _stage_cover(track, stage / "adjunct", stage, fetcher)
                if cover is None and options["extract_audio"]:
                    cover = await _video_cover(
                        track, incoming, stage, fetcher, pacer, already_video=used_video
                    )
                if cover is not None:
                    audio_before_cover = packet_signature(audio)
                    write_isolated_mp4(audio, {}, cover=cover.read_bytes())
                    if packet_signature(audio) != audio_before_cover:
                        raise ValueError("Audio packets changed while embedding cover")
                    from mutagen.mp4 import MP4Cover

                    embedded = MP4(audio).get("covr")
                    if (
                        not embedded
                        or len(embedded) != 1
                        or bytes(embedded[0]) != cover.read_bytes()
                        or embedded[0].imageformat
                        not in {MP4Cover.FORMAT_JPEG, MP4Cover.FORMAT_PNG}
                    ):
                        raise ValueError("Prepared embedded cover verification failed")
                    members.append((cover, Path(str(base) + ".cover" + cover.suffix)))
            if options["lyrics_sidecar"] and track.lyrics:
                lyrics = write_lyrics_sidecar(audio, track.lyrics, options["lyrics_sidecar"])
                if lyrics is not None:
                    members.append((lyrics, Path(str(base) + lyrics.suffix)))
            side = stage / "sidecar.json"
            durable_json(
                side,
                build_sidecar(
                    track,
                    handle=handle,
                    saved_files=[dest.name for _, dest in members],
                    source=options.get("source") or "verified-download",
                ),
            )
            if read_sidecar(side, strict_identity=True) is None:
                raise ValueError("Prepared sidecar cannot be safely indexed")
            members.append((side, Path(str(base) + ".json")))
            prepared_bytes = sum(path.stat().st_size for path, _ in members)
            if options["max_disk_mb"] and (
                total_bytes + prepared_bytes > options["max_disk_mb"] * 1024 * 1024
            ):
                stats["skipped_capped"] += 1
                continue
            incoming.unlink(missing_ok=True)
            for path, _ in members:
                os.chmod(path, 0o644)
                with path.open("rb") as synced_member:
                    os.fsync(synced_member.fileno())
            sync_directory(stage)
            # Let cancellation queued during tagging run before recording the
            # publication intent. Once recorded, publish and report as one step.
            await asyncio.sleep(0)
            marker = intents / (track.id + ".json")
            retain = True
            durable_json(
                marker,
                {
                    "schema": 2,
                    "id": track.id,
                    "profile": subdir or "",
                    "members": [
                        {
                            "stage": path.relative_to(root).as_posix(),
                            "destination": dest.relative_to(root).as_posix(),
                            "sha256": file_hash(path),
                            "inode": [path.stat().st_dev, path.stat().st_ino],
                        }
                        for path, dest in members
                    ],
                },
            )
            publish_prepared(root, marker)
            retain = False
            stats["downloaded"] += 1
            print(ARCHIVED_PREFIX + track.id.lower(), flush=True)
            stats["tagged"] += 1
            stats["sidecars"] += 1
            total_tracks += 1
            total_bytes += prepared_bytes
            print("Downloaded: " + base.name + ".m4a")
        finally:
            if not retain and stage.exists():
                if stage.is_symlink() or not stage.resolve().is_relative_to(stage_parent.resolve()):
                    raise ValueError("Private staging ownership changed")
                shutil.rmtree(stage)
                sync_directory(stage_parent)
    report_capacity(root, [], covers=options["covers"])
    if options.get("source"):
        record_membership(root, options["source"], [t.id for t in active])
    return stats
