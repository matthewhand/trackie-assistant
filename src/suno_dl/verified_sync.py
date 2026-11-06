"""Verified Linux incremental sync with staging before atomic publication.

This path only adds missing UUIDs. It never retags, replaces, prunes or removes
an indexed recording. Every new recording uses its own canonical song page's
producer, is tagged in private staging, and has a recoverable two-file intent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .archive_lock import _exclusive_archive
from .discovery import discover_profile_tracks_feed, fetch_profile_feed_page
from .fetcher import Fetcher
from .library import LibraryIndex, record_membership
from .lossless_mp4 import UnsupportedMP4, write_isolated_mp4
from .media_process import run as run_media_process
from .models import TrackInfo
from .naming import build_filename
from .pacing import PlaybackPacer
from .playlist_api import fetch_public_playlist_api_page
from .playlists import _fetch_public_api_membership, discover_profile_playlists
from .producer_lookup import producer_identity_from_page
from .sidecar import build_sidecar

log = logging.getLogger(__name__)
MAX_MEDIA = 512 * 1024 * 1024


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def raise_if_migration_pending(root: Path) -> None:
    journal = root / ".library" / "producer-rename-journal.json"
    if journal.exists() or journal.is_symlink():
        raise ValueError("producer migration journal requires review before syncing")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("symlink directory")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("invalid directory")
    sync_directory(path.parent)
    return path


def durable_json(path: Path, data: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    sync_directory(path.parent)


def safe_relative(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid intent path")
    part = Path(value)
    if part.is_absolute() or any(x in {".", ".."} for x in part.parts):
        raise ValueError("unsafe intent path")
    result = root
    for item in part.parts:
        result /= item
        if result.is_symlink():
            raise ValueError("symlink intent path")
    if not result.resolve().is_relative_to(root):
        raise ValueError("intent path escapes archive")
    return result


def publish_intent(root: Path, marker: Path) -> str:
    """Publish/recover the deployed pair protocol using the shared publisher."""
    from .publication import publish_prepared

    return publish_prepared(root, marker)


def media_urls(html: str, clip_id: str) -> tuple[list[str], TrackInfo | None]:
    from .published_media import published_sources

    return published_sources(html, clip_id)


def packet_signature(path: Path) -> bytes:
    # Hash streamed packet evidence on disk, rather than retaining an
    # attacker-controlled number of packet rows in memory.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        run_media_process(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_packets",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "packet=size,data_hash",
                "-of",
                "compact=p=0:nk=0",
                str(path),
            ],
            check=True,
            stdout=output,
            stderr=errors,
            timeout=120,
        )
        size = output.tell()
        if not 0 < size <= 256 * 1024 * 1024:
            raise ValueError("missing or excessive audio packet evidence")
        output.seek(0)
        h = hashlib.sha256()
        for chunk in iter(lambda: output.read(1024 * 1024), b""):
            h.update(chunk)
        return h.digest()


def prepare_audio(
    source: Path, output: Path, artist: str, title: str, *, allow_extraction: bool = True
) -> None:
    before = packet_signature(source)
    probe = run_media_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    streams = json.loads(probe.stdout)["streams"]
    if sum(item.get("codec_type") == "audio" for item in streams) != 1:
        raise ValueError("only one audio stream is supported")
    values = {b"\xa9ART": artist, b"\xa9nam": title}
    direct = False
    if len(streams) == 1:
        # Existing audio MP4 metadata must not pass through a general remux.
        shutil.copyfile(source, output)
        try:
            write_isolated_mp4(output, values)
            direct = True
        except UnsupportedMP4:
            output.unlink()
            # Packet equality cannot prove opaque metadata survives a remux.
            # An unsupported audio-only layout is not permission to convert it.
            raise
    if not direct and not allow_extraction:
        raise ValueError("Audio extraction requires explicit opt-in")
    if not direct:
        # A video or unsupported input container needs the explicitly requested
        # audio extraction. Never re-encode the audio stream.
        run_media_process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "copy",
                "-f",
                "ipod",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        if packet_signature(output) != before:
            raise ValueError("audio packets changed during extraction")
        write_isolated_mp4(output, values)
    from mutagen.mp4 import MP4

    tags = MP4(output)
    if tags.get("\xa9ART") != [artist] or tags.get("\xa9nam") != [title]:
        raise ValueError("prepared tags do not match")
    if packet_signature(output) != before:
        raise ValueError("audio packets changed during tagging")
    # Packet equality proves preservation, but encrypted/corrupt AAC packets
    # can still demux. Require actual decoding before publication.
    with tempfile.TemporaryFile() as errors:
        run_media_process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(output),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            timeout=180,
        )
    os.chmod(output, 0o644)
    with output.open("rb") as f:
        os.fsync(f.fileno())


async def inventory(profile: str, fetcher: Fetcher) -> tuple[list[TrackInfo], dict[str, list[str]]]:
    tracks = await discover_profile_tracks_feed(profile)
    first = await fetch_profile_feed_page(
        fetcher.client, profile, 1, rate_limiter=fetcher.rate_limiter
    )
    total = first.get("num_total_clips")
    if type(total) is not int or len(tracks) != total:
        raise ValueError("profile clip inventory does not match its reported total")
    members = {"profile:" + profile: [t.id for t in tracks]}

    async def feed(page: int):
        return await fetch_profile_feed_page(
            fetcher.client, profile, page, rate_limiter=fetcher.rate_limiter
        )

    listing = await discover_profile_playlists(feed)
    if listing.truncated:
        raise ValueError(listing.reason or "public playlist listing is incomplete")
    by_id = {t.id: t for t in tracks}
    playlist_order: list[str] = []
    excluded: set[str] = set()
    for playlist in listing.playlists:

        async def api(ref, page):
            return await fetch_public_playlist_api_page(
                ref.id, page, timeout_sec=30, rate_limiter=fetcher.rate_limiter
            )

        clips = await _fetch_public_api_membership(api, playlist, on_excluded=excluded.add)
        members["playlist:" + playlist.id] = [t.id for t in clips]
        playlist_order.extend(t.id for t in clips)
        by_id.update({t.id: t for t in clips if t.id not in by_id})
        log.info("Playlist %s: %d verified membership rows", playlist.name, len(clips))
    ordered_ids = dict.fromkeys([*playlist_order, *by_id])
    if excluded:
        log.warning("Excluded %d non-public or visibility-ambiguous clip IDs", len(excluded))
        members = {
            source: [clip_id for clip_id in ids if clip_id not in excluded]
            for source, ids in members.items()
        }
    return [by_id[clip_id] for clip_id in ordered_ids if clip_id not in excluded], members


def require_owned_library(root: Path, index: LibraryIndex) -> None:
    owned: set[Path] = set()
    for clip_id in index.ids():
        entry = index.get(clip_id)
        if entry is None:
            raise ValueError("library UUID disappeared during ownership validation")
        owned.update(path.resolve() for path in entry.media)

    def failed_inventory(exc):
        raise ValueError("Cannot inspect the entire library") from exc

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed_inventory):
        for name in list(dirs):
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("symlink library directory")
            if name in {".library", ".suno-staging", ".git"}:
                dirs.remove(name)
        for name in files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("symlink library member")
            if path.suffix.lower() in {
                ".m4a",
                ".mp3",
                ".mp4",
                ".wav",
                ".flac",
                ".ogg",
                ".aac",
                ".m4b",
            } and (path.resolve() not in owned or path.stat().st_nlink != 1):
                raise ValueError("unowned or linked library media: " + str(path))


async def run(root: Path, profile: str, *, apply: bool, audit: Path, min_free_mb: int) -> None:
    if not sys.platform.startswith("linux") or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("verified sync currently requires Linux and a profile handle")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid library root")
    root = root.resolve()
    async with Fetcher(30, 2, rate_limit_sec=1.5) as fetcher:
        tracks, members = await inventory(profile, fetcher)
        index = LibraryIndex(root).scan()
        if index.conflicts:
            raise ValueError("conflicting library UUID ownership")
        if not apply:
            require_owned_library(root, index)
        missing = [t for t in tracks if index.get(t.id) is None]
        log.info(
            "Inventory: unique=%d existing=%d missing=%d playlists=%d",
            len(tracks),
            len(tracks) - len(missing),
            len(missing),
            len(members) - 1,
        )
        if not apply:
            return
        async with _exclusive_archive(root):
            raise_if_migration_pending(root)
            out = directory(root / profile)
            internals = directory(root / ".library")
            intents = directory(internals / "verified-sync-intents")
            stages = directory(directory(root / ".suno-staging") / "verified-sync")
            audit.parent.mkdir(parents=True, exist_ok=True)

            def event(kind: str, **values):
                record = {"at": datetime.now(UTC).isoformat(), "event": kind, **values}
                with audit.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                log.info("%s %s", kind, json.dumps(values, ensure_ascii=False))

            for marker in sorted(intents.glob("*.json")):
                event("recovered", id=publish_intent(root, marker))
            index = LibraryIndex(root).scan()
            if index.conflicts:
                raise ValueError("conflicting library ownership after recovery")
            require_owned_library(root, index)
            missing = [t for t in tracks if index.get(t.id) is None]
            for source, ids in members.items():
                record_membership(root, source, ids)
            event(
                "started",
                id=str(uuid.uuid4()),
                sources=len(members),
                total=len(tracks),
                existing=len(tracks) - len(missing),
                missing=len(missing),
            )
            pacer = PlaybackPacer()
            downloaded = failed = 0
            for t in missing:
                # Legacy uncertain downloads must be examined before retrying.
                legacy = internals / "download-intents" / (t.id + ".json")
                if legacy.exists() or legacy.is_symlink():
                    raise ValueError("legacy interrupted download requires review: " + t.id)
                html = await fetcher.fetch_song_page(t.id)
                producer = producer_identity_from_page(t.id, html)
                t.artist, t.creator_handle, t.title = (
                    producer.artist,
                    producer.handle,
                    producer.title,
                )
                urls, parsed = media_urls(html, t.id)
                if parsed:
                    t.duration_sec = parsed.duration_sec
                base_name = build_filename(
                    "{artist}_{title}", title=t.title, handle=profile, clip_id=t.id, artist=t.artist
                )
                # Include UUID only for a real collision; never infer ownership
                # from a filename or reuse an unindexed recording.
                base_name = base_name.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(" ._")
                base = out / base_name
                occupied = {p.name.casefold() for p in out.iterdir()}
                if any(
                    (base.name + ext).casefold() in occupied
                    for ext in (".m4a", ".json", ".mp3", ".mp4", ".wav")
                ):
                    base = out / (
                        base_name.encode("utf-8")[:140].decode("utf-8", "ignore") + "_" + t.id
                    )
                if any(
                    (base.name + ext).casefold() in occupied
                    for ext in (".m4a", ".json", ".mp3", ".mp4", ".wav")
                ):
                    raise ValueError("unowned filename collision: " + t.id)
                if shutil.disk_usage(root).free < min_free_mb * 1024 * 1024 + MAX_MEDIA * 2:
                    raise ValueError("library free-space reserve reached")
                stage = Path(tempfile.mkdtemp(prefix=t.id + "-", dir=stages))
                incoming, audio = stage / "incoming", stage / "audio.part"
                await pacer.wait(t.duration_sec)
                ready = False
                for url in urls:
                    try:
                        incoming.unlink(missing_ok=True)
                        audio.unlink(missing_ok=True)
                        await fetcher.rate_limiter.acquire(url)
                        async with fetcher.client.stream(
                            "GET",
                            url,
                            follow_redirects=False,
                            headers={"Referer": "https://suno.com/", "Origin": "https://suno.com"},
                        ) as response:
                            if response.status_code != 200:
                                continue
                            count = 0
                            with incoming.open("xb") as f:
                                async for chunk in response.aiter_bytes(64 * 1024):
                                    count += len(chunk)
                                    if count > MAX_MEDIA:
                                        raise ValueError("media exceeds size cap")
                                    f.write(chunk)
                                f.flush()
                                os.fsync(f.fileno())
                        prepare_audio(incoming, audio, t.artist, t.title)
                        ready = True
                        break
                    except (httpx.HTTPError, subprocess.SubprocessError, ValueError, OSError):
                        # URLs can contain credentials, so failures log the UUID only.
                        continue
                if not ready:
                    failed += 1
                    event("unavailable", id=t.id)
                    incoming.unlink(missing_ok=True)
                    audio.unlink(missing_ok=True)
                    stage.rmdir()
                    continue
                final_audio = Path(str(base) + ".m4a")
                final_side = Path(str(base) + ".json")
                side = stage / "sidecar.json"
                durable_json(
                    side,
                    build_sidecar(
                        t,
                        handle=profile,
                        saved_files=[final_audio.name],
                        source="verified-download",
                    ),
                )
                os.chmod(side, 0o644)
                with side.open("rb") as side_stream:
                    os.fsync(side_stream.fileno())
                sync_directory(stage)
                marker = intents / (t.id + ".json")
                durable_json(
                    marker,
                    {
                        "schema": 1,
                        "id": t.id,
                        "profile": profile,
                        "members": [
                            {
                                "stage": p.relative_to(root).as_posix(),
                                "destination": dest.relative_to(root).as_posix(),
                                "sha256": digest(p),
                                "inode": [p.stat().st_dev, p.stat().st_ino],
                            }
                            for p, dest in [(audio, final_audio), (side, final_side)]
                        ],
                    },
                )
                publish_intent(root, marker)
                incoming.unlink(missing_ok=True)
                stage.rmdir()
                sync_directory(stages)
                downloaded += 1
                event(
                    "downloaded",
                    id=t.id,
                    artist=t.artist,
                    creator_handle=t.creator_handle,
                    filename=final_audio.name,
                    sha256=digest(final_audio),
                )
            event("finished", downloaded=downloaded, failed=failed, total=len(tracks))
            if failed:
                raise RuntimeError(
                    str(failed) + " public recordings were unavailable; audit retained"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--min-free-mb", type=int, default=20480)
    args = parser.parse_args()
    if args.min_free_mb < 2048:
        parser.error("free-space reserve must be at least 2048 MB")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(
        run(
            args.library,
            args.profile,
            apply=args.apply,
            audit=args.audit,
            min_free_mb=args.min_free_mb,
        )
    )


if __name__ == "__main__":
    main()
