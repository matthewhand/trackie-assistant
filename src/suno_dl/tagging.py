"""Embedded artist/title tags for archived tracks.

Why this exists
---------------
Music Assistant (and most tag readers) show a file with no tags as
``[unknown]`` / ``[unknown title]`` and, worse, *guess* the title from the
filename — splitting on the first ``.`` and treating the rest as an extension.
So the tool writes the metadata itself:

* ``artist`` = the profile handle the track was synced from (``drinko``)
* ``title``  = the full track title, punctuation and ``feat.`` included

Embedded tag updates are currently blocked because preservation of opaque
metadata has not been established. Read-only inspection and dry runs remain
available. Both staged replacement paths refuse writes.

Tagging ordinarily fails softly. If replacement happens but its directory
cannot be synced, a TagPublicationUncertain error stops the operation: the
caller must inspect the affected file before retrying.

One-shot migration for an existing archive lives in
:func:`retag_tree` (surfaced as ``suno-dl --retag``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import find_ffmpeg
from .media_process import run as run_media_process

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIO_SUFFIXES",
    "TagResult",
    "TagPublicationUncertain",
    "read_tags",
    "retag_tree",
    "tag_file",
    "tags_from_stem",
    "untagged_files",
    "write_lyrics_sidecar",
]

# Suffixes we consider playable audio. A stem carrying ``.cover.jpg`` is not
# one of them, so the cover art a ``--covers`` run leaves behind is never
# retagged.
AUDIO_SUFFIXES = frozenset({".m4a", ".mp4", ".m4b", ".aac", ".mp3", ".wav", ".flac", ".ogg"})

_MP4_SUFFIXES = frozenset({".m4a", ".mp4", ".m4b", ".aac"})

# A lyrics sidecar mirrors the embedded tag; refuse an unbounded one (#157).
# 64 KiB is roughly 8k words, far beyond any song lyric.
MAX_LYRICS_BYTES = 64 * 1024


class TaggingUnavailable(RuntimeError):
    """Embedded metadata cannot currently be written without risking loss."""


def require_embedded_tag_writes() -> None:
    """Refuse an operation before publishing media that needs embedded tags."""
    reason = _embedded_tag_write_guard()
    if reason:
        raise TaggingUnavailable(reason)


class TagPublicationUncertain(RuntimeError):
    """Publication may have happened, but its outcome is uncertain."""


@dataclass(frozen=True)
class TagResult:
    """Outcome of one tag write."""

    tagged: bool
    backend: str
    artist: str
    title: str
    lyrics: str | None = None
    reason: str = ""


def _tag_wav(path: Path, artist: str, title: str) -> None:
    """WAV stores ID3 frames, not mutagen's easy key/value mapping."""
    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE

    audio = WAVE(path)
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    if tags is None:  # pragma: no cover — mutagen always creates them here
        raise ValueError("could not create ID3 tags")
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    audio.save()


def _write_with_mutagen(
    path: Path,
    artist: str,
    title: str,
    lyrics: str | None = None,
    format_suffix: str | None = None,
) -> tuple[bool, str]:
    """Write tags to an already isolated file; callers must stage it first."""
    try:
        from mutagen import File  # lazy: optional dependency
        from mutagen.mp4 import MP4  # for ©lyr tag
    except ImportError:
        return False, "mutagen not installed"
    try:
        suffix = (format_suffix or path.suffix).lower()
        if suffix == ".wav":
            _tag_wav(path, artist, title)
        elif suffix in _MP4_SUFFIXES:
            # For MP4/M4A, use MP4 directly to support ©lyr tag
            mp4 = MP4(path)
            if mp4.tags is None:
                mp4.add_tags()
            mp4["\xa9ART"] = [artist]  # artist
            mp4["\xa9nam"] = [title]  # title
            if lyrics and lyrics.strip():
                mp4["\xa9lyr"] = [lyrics.strip()]
            mp4.save()
        else:
            # For other formats, use easy=True
            audio = File(path, easy=True)
            if audio is None or audio.tags is None:
                return False, "unsupported container"
            audio["artist"] = [artist]
            audio["title"] = [title]
            audio.save()
    except Exception as exc:  # noqa: BLE001 — a bad tag must never fail a download
        return False, f"mutagen: {exc}"
    return True, "mutagen"


def _ffmpeg_integrity_evidence(
    path: Path, packet_path: Path, *, lyrics_updated: bool
) -> tuple[dict[str, str], dict[str, object], str]:
    """Inspect native tags, stream topology and packet data before a remux.

    This checks what Mutagen and ffprobe expose. Opaque container atoms that
    neither tool reports cannot be proven preserved by this fallback.
    """
    from mutagen import File

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ValueError("ffprobe is required to verify the remux")
    try:
        media = File(path)
    except Exception as exc:  # noqa: BLE001 - unsupported media must fail closed
        raise ValueError("Mutagen cannot inspect the media") from exc
    if media is None:
        raise ValueError("Mutagen cannot inspect the media")
    changed_tags = {"artist", "title", "\xa9art", "\xa9nam", "tpe1", "tit2"}
    if lyrics_updated:
        changed_tags.update({"lyrics", "\xa9lyr", "uslt"})
    native = {
        str(key): repr(value)
        for key, value in (media.tags or {}).items()
        if str(key).casefold() not in changed_tags
        and not (lyrics_updated and str(key).casefold().startswith("uslt:"))
    }
    probe = run_media_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    data = json.loads(probe.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise ValueError("ffprobe did not return stream data")

    def preserved_tags(value: object, *, exclude_changed: bool) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key).casefold(): str(item)
            for key, item in value.items()
            if not exclude_changed or str(key).casefold() not in changed_tags
        }

    streams = []
    for stream in data["streams"]:
        if not isinstance(stream, dict):
            raise ValueError("invalid stream data")
        streams.append(
            {
                "index": stream.get("index"),
                "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"),
                "codec_tag_string": stream.get("codec_tag_string"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "disposition": stream.get("disposition", {}),
                "tags": preserved_tags(stream.get("tags"), exclude_changed=False),
            }
        )
    chapters = []
    for chapter in data.get("chapters", []):
        if not isinstance(chapter, dict):
            raise ValueError("invalid chapter data")
        chapters.append(
            {
                "start_time": chapter.get("start_time"),
                "end_time": chapter.get("end_time"),
                "tags": preserved_tags(chapter.get("tags"), exclude_changed=False),
            }
        )
    format_info = data.get("format", {})
    if not isinstance(format_info, dict):
        raise ValueError("invalid format data")
    details: dict[str, object] = {
        "streams": streams,
        "chapters": chapters,
        "format_tags": preserved_tags(format_info.get("tags"), exclude_changed=True),
    }
    with packet_path.open("xb") as packets:
        run_media_process(
            [
                ffprobe,
                "-v",
                "error",
                "-show_packets",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "packet=stream_index,size,data_hash",
                "-of",
                "compact=p=0:nk=0",
                str(path),
            ],
            check=True,
            stdout=packets,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    digest = hashlib.sha256()
    with packet_path.open("rb") as packet_reader:
        for chunk in iter(lambda: packet_reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return native, details, digest.hexdigest()


def _ffmpeg_evidence_preserved(
    original: tuple[dict[str, str], dict[str, object], str],
    result: tuple[dict[str, str], dict[str, object], str],
) -> bool:
    old_tags, old_details, old_packets = original
    new_tags, new_details, new_packets = result
    if old_packets != new_packets:
        return False
    if any(new_tags.get(key) != value for key, value in old_tags.items()):
        return False
    if old_details["streams"] != new_details["streams"]:
        return False
    if old_details["chapters"] != new_details["chapters"]:
        return False
    old_format = old_details["format_tags"]
    new_format = new_details["format_tags"]
    if not isinstance(old_format, dict) or not isinstance(new_format, dict):
        return False
    return all(new_format.get(key) == value for key, value in old_format.items())


def _embedded_tag_write_guard() -> str | None:
    """Block media replacement until all unrelated metadata can be preserved."""
    return (
        "Embedded tag updates are unavailable: preservation of existing metadata "
        "is not verified; manual handling is required"
    )


def _write_with_ffmpeg(
    path: Path, artist: str, title: str, lyrics: str | None = None
) -> tuple[bool, str]:
    """Remux a verified source in a private sibling directory before replacement.

    The final identity check and replace are separate operations. Callers must
    exclude noncooperating writers that could swap the destination between them.
    """
    reason = _embedded_tag_write_guard()
    if reason:
        return False, reason
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return False, "ffmpeg not found"
    try:
        import mutagen
    except ImportError:
        return False, "Mutagen is required to verify the ffmpeg output"
    if not callable(getattr(mutagen, "File", None)):
        return False, "Mutagen cannot inspect the ffmpeg output"
    if shutil.which("ffprobe") is None:
        return False, "ffprobe is required to verify the ffmpeg output"
    if not hasattr(os, "listxattr"):
        return False, "cannot verify source extended attributes"
    if not hasattr(os, "O_DIRECTORY"):
        return False, "cannot verify directory durability"
    muxers = {
        ".m4a": "ipod",
        ".m4b": "ipod",
        ".mp4": "mp4",
        ".aac": "adts",
        ".mp3": "mp3",
        ".wav": "wav",
        ".flac": "flac",
        ".ogg": "ogg",
    }
    muxer = muxers.get(path.suffix.lower())
    if muxer is None:
        return False, "unsupported ffmpeg output format"
    source_fd: int | None = None
    parent_fd: int | None = None
    stage: tempfile.TemporaryDirectory[str] | None = None

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            info.st_nlink,
        )

    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(parent_fd)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & 0x400
        ):
            return False, "unsafe linked or non-regular source"
        source_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(source_fd)
        if identity(opened) != identity(before):
            return False, "source changed before tagging"
        if hasattr(os, "listxattr") and os.listxattr(source_fd):
            return False, "source extended attributes require manual handling"
        stage = tempfile.TemporaryDirectory(prefix=".suno-tag-", dir=path.parent)
        stage_path = Path(stage.name)
        source_copy = stage_path / "source.part"
        output = stage_path / "output.part"
        with os.fdopen(os.dup(source_fd), "rb") as original, source_copy.open("xb") as copied:
            shutil.copyfileobj(original, copied, length=1024 * 1024)
        if identity(os.fstat(source_fd)) != identity(before):
            return False, "source changed while copying"
        cmd = [
            ffmpeg,
            "-v",
            "error",
            "-n",
            "-i",
            str(source_copy),
            "-map",
            "0",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c",
            "copy",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"title={title}",
        ]
        if lyrics and lyrics.strip():
            cmd.extend(["-metadata", f"lyrics={lyrics.strip()}"])
        cmd.extend(["-f", muxer, str(output)])
        try:
            run_media_process(cmd, check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"ffmpeg: {exc}"
        try:
            output_info = output.lstat()
        except FileNotFoundError:
            return False, "ffmpeg produced no output"
        if (
            not stat.S_ISREG(output_info.st_mode)
            or output_info.st_nlink != 1
            or output_info.st_size == 0
            or getattr(output_info, "st_file_attributes", 0) & 0x400
        ):
            return False, "ffmpeg produced unsafe output"
        try:
            original_evidence = _ffmpeg_integrity_evidence(
                source_copy,
                stage_path / "source-packets.part",
                lyrics_updated=bool(lyrics and lyrics.strip()),
            )
            output_evidence = _ffmpeg_integrity_evidence(
                output,
                stage_path / "output-packets.part",
                lyrics_updated=bool(lyrics and lyrics.strip()),
            )
        except (ImportError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return False, f"cannot verify ffmpeg output: {exc}"
        if not _ffmpeg_evidence_preserved(original_evidence, output_evidence):
            return False, "ffmpeg output changed streams, packets or existing metadata"
        output_fd = os.open(output, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            result = os.fstat(output_fd)
            if identity(result) != identity(output_info):
                return False, "ffmpeg output changed before publication"
            if (result.st_uid, result.st_gid) != (before.st_uid, before.st_gid):
                if not hasattr(os, "fchown"):
                    return False, "cannot preserve source ownership"
                os.fchown(output_fd, before.st_uid, before.st_gid)
            if hasattr(os, "fchmod"):
                os.fchmod(output_fd, stat.S_IMODE(before.st_mode))
            else:
                os.chmod(output, stat.S_IMODE(before.st_mode))
            result = os.fstat(output_fd)
            if (result.st_uid, result.st_gid, stat.S_IMODE(result.st_mode)) != (
                before.st_uid,
                before.st_gid,
                stat.S_IMODE(before.st_mode),
            ) or result.st_nlink != 1:
                return False, "could not preserve source ownership or permissions"
            if hasattr(os, "listxattr") and os.listxattr(output_fd):
                return False, "output extended attributes require manual handling"
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        stage_fd = os.open(stage_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        if hasattr(os, "listxattr") and os.listxattr(source_fd):
            return False, "source extended attributes changed"
        current = path.lstat()
        if identity(current) != identity(before) or identity(os.fstat(source_fd)) != identity(
            before
        ):
            return False, "source changed before replacement"
        # Release the read handle before replacement for platforms that deny
        # replacing an open file. The caller must prevent noncooperating swaps.
        os.close(source_fd)
        source_fd = None
        os.replace(output, path)
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            # Replacement already happened. A journal would be required to
            # guarantee rollback; report the durability uncertainty honestly.
            logger.warning("Tagged file replacement could not be synced: %s", exc)
        return True, "ffmpeg"
    except (OSError, TypeError, NotImplementedError) as exc:
        return False, f"ffmpeg: {exc}"
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if stage is not None:
            try:
                stage.cleanup()
            except OSError as exc:
                logger.warning("Could not remove private tag staging directory: %s", exc)


def _preflight_tag_allocation(fd: int, size: int, parent: Path, tag_bytes: int) -> None:
    """Fail closed on detectable holes and insufficient free staging space.

    Some filesystems report a whole file as data even if it contains holes;
    neither SEEK_HOLE nor st_blocks proves allocation topology universally.
    Free space is a snapshot, not a reservation against concurrent writers.
    """
    if not hasattr(os, "SEEK_DATA") or not hasattr(os, "SEEK_HOLE"):
        raise ValueError("cannot verify source allocation")
    info = os.fstat(fd)
    if not hasattr(info, "st_blocks") or info.st_blocks * 512 < size:
        raise ValueError("sparse or unverifiable source allocation")
    try:
        if size:
            data = os.lseek(fd, 0, os.SEEK_DATA)
            hole = os.lseek(fd, 0, os.SEEK_HOLE)
            if data != 0 or hole < size:
                raise ValueError("sparse source requires manual handling")
    finally:
        os.lseek(fd, 0, os.SEEK_SET)
    margin = max(64 * 1024 * 1024, (size + 9) // 10, tag_bytes * 2)
    if shutil.disk_usage(parent).free < size + margin:
        raise ValueError("insufficient staging space")


def _write_staged_mutagen(
    path: Path, artist: str, title: str, lyrics: str | None
) -> tuple[bool, str]:
    """Stage a Mutagen edit and publish only after safety checks.

    This requires Linux filesystem metadata inspection and directory fsync.
    All writers, including noncooperating processes, must be excluded: the
    final path identity check and replace are separate operations. Mutagen
    may still change opaque container metadata; this is not a proof of
    complete metadata preservation.
    """
    reason = _embedded_tag_write_guard()
    if reason:
        return False, reason
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "listxattr", "fchmod")):
        return False, "cannot verify filesystem metadata and durability"
    from .producer_migration import _default_inode_flags

    source_fd = parent_fd = staged_fd = None
    stage = None
    published = False

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            info.st_nlink,
        )

    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(parent_fd)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return False, "unsafe linked or non-regular source"
        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if identity(os.fstat(source_fd)) != identity(before):
            return False, "source changed before tagging"
        if os.listxattr(source_fd):
            return False, "source extended attributes require manual handling"
        source_flags = _default_inode_flags(path)
        tag_bytes = sum(len(value.encode("utf-8")) for value in (artist, title, lyrics or ""))
        _preflight_tag_allocation(source_fd, before.st_size, path.parent, tag_bytes)
        stage = tempfile.TemporaryDirectory(prefix=".suno-tag-", dir=path.parent)
        stage_dir = Path(stage.name)
        output = stage_dir / "audio.part"
        with os.fdopen(os.dup(source_fd), "rb") as original, output.open("xb") as copied:
            remaining = before.st_size
            while remaining:
                chunk = original.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return False, "source changed while copying"
                copied.write(chunk)
                remaining -= len(chunk)
            if original.read(1):
                return False, "source changed while copying"
        if identity(os.fstat(source_fd)) != identity(before):
            return False, "source changed while copying"
        ok, detail = _write_with_mutagen(output, artist, title, lyrics, path.suffix)
        if not ok:
            return False, detail
        stage_info = output.lstat()
        if (
            not stat.S_ISREG(stage_info.st_mode)
            or stage_info.st_nlink != 1
            or not stage_info.st_size
        ):
            return False, "unsafe staged output"
        staged_fd = os.open(output, os.O_RDWR | os.O_NOFOLLOW)
        if identity(os.fstat(staged_fd)) != identity(stage_info):
            return False, "staged output changed"
        if (stage_info.st_uid, stage_info.st_gid) != (before.st_uid, before.st_gid):
            if not hasattr(os, "fchown"):
                return False, "cannot preserve source ownership"
            os.fchown(staged_fd, before.st_uid, before.st_gid)
        os.fchmod(staged_fd, stat.S_IMODE(before.st_mode))
        stage_info = os.fstat(staged_fd)
        if (stage_info.st_uid, stage_info.st_gid, stat.S_IMODE(stage_info.st_mode)) != (
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        ) or stage_info.st_nlink != 1:
            return False, "cannot preserve source ownership or permissions"
        if os.listxattr(staged_fd):
            return False, "staged extended attributes require manual handling"
        if _default_inode_flags(output) != source_flags:
            return False, "staged inode flags differ"
        # Reopen using the intended type rather than the non-media staging suffix.
        from mutagen import File
        from mutagen.mp4 import MP4

        if path.suffix.lower() in _MP4_SUFFIXES:
            media = MP4(output)
            if list(media.get("\xa9ART", [])) != [artist] or list(media.get("\xa9nam", [])) != [
                title
            ]:
                return False, "staged tags do not match"
            if lyrics and lyrics.strip() and list(media.get("\xa9lyr", [])) != [lyrics.strip()]:
                return False, "staged lyrics do not match"
        elif path.suffix.lower() == ".wav":
            from mutagen.wave import WAVE

            wave_tags = WAVE(output).tags
            if (
                wave_tags is None
                or str(wave_tags.get("TPE1", "")) != artist
                or str(wave_tags.get("TIT2", "")) != title
            ):
                return False, "staged tags do not match"
        else:
            easy_media = File(output, easy=True)
            if (
                easy_media is None
                or list(easy_media.get("artist", [])) != [artist]
                or list(easy_media.get("title", [])) != [title]
            ):
                return False, "staged tags do not match"
        if identity(output.lstat()) != identity(stage_info) or identity(
            os.fstat(staged_fd)
        ) != identity(stage_info):
            return False, "staged output changed before publication"
        os.fsync(staged_fd)
        stage_fd = os.open(stage_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        if os.listxattr(source_fd) or _default_inode_flags(path) != source_flags:
            return False, "source filesystem metadata changed"
        if identity(path.lstat()) != identity(before) or identity(os.fstat(source_fd)) != identity(
            before
        ):
            return False, "source changed before replacement"
        if identity(output.lstat()) != identity(stage_info) or identity(
            os.fstat(staged_fd)
        ) != identity(stage_info):
            return False, "staged output changed before replacement"
        try:
            os.replace(output, path)
        except Exception as exc:  # noqa: BLE001 - replacement may have taken effect
            raise TagPublicationUncertain(
                f"Tagged file replacement did not complete cleanly: {path}; "
                "inspect the file before retrying"
            ) from exc
        published = True
        try:
            os.fsync(parent_fd)
        except Exception as exc:  # noqa: BLE001 - replacement has already happened
            raise TagPublicationUncertain(
                f"Tagged file was replaced but its directory could not be synced: {path}; "
                "inspect the file before retrying"
            ) from exc
        return True, "mutagen"
    except TagPublicationUncertain:
        raise
    except Exception as exc:  # noqa: BLE001 - tagging is best effort
        return False, f"mutagen staging: {exc}"
    finally:
        pending = sys.exception()
        cleanup_errors = []
        for fd in (staged_fd, source_fd, parent_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception as exc:  # noqa: BLE001 - attempt every cleanup
                    cleanup_errors.append(exc)
        if stage is not None:
            try:
                stage.cleanup()
            except Exception as exc:  # noqa: BLE001 - preserve prior publication error
                cleanup_errors.append(exc)
        if cleanup_errors:
            logger.warning("Tag staging cleanup failed: %s", cleanup_errors)
            if published and pending is None:
                raise TagPublicationUncertain(
                    f"Tagged file was replaced but cleanup failed: {path}; "
                    "inspect the file before retrying"
                )


def tag_file(path: Path, *, artist: str, title: str, lyrics: str | None = None) -> TagResult:
    """Request embedded tags; updates are blocked until preservation is verified.

    The caller must exclude noncooperating writers through final replacement
    and is responsible for independently verifying any supplied producer.
    Raises TagPublicationUncertain when replacement happened but directory
    durability is unknown; stop and inspect rather than retry automatically.
    """
    if not artist or not title:
        return TagResult(False, "none", artist, title, lyrics, "empty artist or title")
    if not path.is_file():
        return TagResult(False, "none", artist, title, lyrics, f"no such file: {path.name}")
    ok, detail = _write_staged_mutagen(path, artist, title, lyrics)
    if ok:
        return TagResult(True, detail, artist, title, lyrics)
    logger.debug("Tagging %s failed: %s", path.name, detail)
    return TagResult(False, "none", artist, title, lyrics, detail)


def read_tags(path: Path) -> dict[str, str]:
    """Return ``{field: value}`` for a tagged file (``{}`` when untagged/unreadable).

    Uses mutagen's easy=True for standard tags, and also reads the ©lyr tag
    for MP4 files which easy=True doesn't expose.
    """
    try:
        from mutagen import File  # lazy: optional dependency
        from mutagen.mp4 import MP4
    except ImportError:
        return {}
    try:
        if path.suffix.lower() == ".wav":
            from mutagen.wave import WAVE

            tags = WAVE(path).tags
            if tags is None:
                return {}
            result: dict[str, str] = {}
            for frame_id, key in (("TPE1", "artist"), ("TIT2", "title")):
                frame = tags.get(frame_id)
                values = getattr(frame, "text", None)
                if isinstance(values, list) and values:
                    result[key] = str(values[0])
            return result
        # First, get standard tags via easy=True
        audio = File(path, easy=True)
        if audio is None or audio.tags is None:
            return {}
        out: dict[str, str] = {}
        for key, value in audio.tags.items():
            if isinstance(value, list) and value:
                out[str(key).lower()] = str(value[0])

        # For MP4 files, also read the ©lyr tag which easy=True doesn't expose
        if path.suffix.lower() in _MP4_SUFFIXES:
            mp4 = MP4(path)
            if mp4 is not None and mp4.tags is not None:
                lyr = mp4.tags.get("\xa9lyr")
                if lyr and isinstance(lyr, list) and lyr:
                    out["lyrics"] = str(lyr[0])
        return out
    except Exception:  # noqa: BLE001 — unreadable file, not an error worth raising
        return {}


def tags_from_stem(stem: str) -> tuple[str, str]:
    """Split ``handle_title`` (the default pattern) into ``(artist, title)``.

    Only the *first* underscore separates the two, so a title keeps its own
    underscores — including the MA-safe ``feat_`` written by newer runs, which
    is deliberately not guessed back into ``feat.``. Titles from the pre-1.6
    archive keep their periods and come back exactly as they were on disk.
    """
    handle, sep, title = stem.partition("_")
    if not sep or not title:
        return "", stem
    return handle, title


def untagged_files(root: Path) -> list[Path]:
    """Return the audio files under ``root`` that carry no title tag."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if not read_tags(path).get("title"):
            found.append(path)
    return found


def write_lyrics_sidecar(audio_path: Path, lyrics: str, format: str) -> Path | None:
    """Write lyrics to a sidecar file next to the audio file.

    ``format`` can be "lrc" or "txt". Returns the sidecar path on success,
    ``None`` on failure (logged, never raises).
    """
    if not lyrics or not lyrics.strip():
        return None
    if format not in ("lrc", "txt"):
        logger.warning("Unknown lyrics sidecar format: %s", format)
        return None
    payload = lyrics.strip()
    if len(payload.encode("utf-8", errors="ignore")) > MAX_LYRICS_BYTES:
        logger.warning("Refusing to write a lyrics sidecar larger than %d bytes", MAX_LYRICS_BYTES)
        return None
    suffix = f".{format}"
    sidecar_path = audio_path.with_suffix(suffix)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=sidecar_path.parent,
            prefix=".suno-lyrics-",
            suffix=".part",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.link(temporary, sidecar_path, follow_symlinks=False)
        logger.debug("Wrote lyrics sidecar: %s", sidecar_path.name)
        return sidecar_path
    except OSError as exc:
        logger.warning("Could not safely write lyrics sidecar %s: %s", sidecar_path.name, exc)
        return None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def retag_tree(
    root: Path,
    *,
    artist: str = "",
    dry_run: bool = False,
    lyrics_sidecar: str | None = None,
) -> dict[str, int]:
    """Tag every untagged audio file under ``root`` in place — one-shot migration.

    ``artist`` overrides the handle inferred from the filename. Nothing is
    downloaded, nothing is renamed, and already-tagged files are left alone,
    so the archive Music Assistant already indexed keeps its paths and play
    counts. Returns ``{scanned, tagged, skipped, failed}``.

    If ``lyrics_sidecar`` is "lrc" or "txt", also write sidecar lyrics files
    for tracks that have lyrics in their tags.
    """
    if not artist:
        # An inferred filename prefix is not verified producer evidence.
        from .producer_lookup import refuse_filename_attribution

        refuse_filename_attribution()
    stats = {"scanned": 0, "tagged": 0, "skipped": 0, "failed": 0}
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Unsafe retag target directory")
    bound = root.resolve(strict=True)
    candidates: list[Path] = []

    def walk_error(exc: OSError) -> None:
        raise RuntimeError("Cannot safely inventory retag target") from exc

    # Preflight the resolved target tree before changing anything, including
    # links and reparse points inside it. CLI target aliases are resolved and
    # must remain within the declared authoritative archive root.
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(directory)
        for name in dirs:
            path = folder / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RuntimeError("Unsafe linked directory in retag target")
        for name in files:
            path = folder / name
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not path.resolve(strict=True).is_relative_to(bound)
            ):
                raise RuntimeError("Unsafe linked audio in retag target")
            candidates.append(path)
    for path in sorted(candidates):
        stats["scanned"] += 1
        tags = read_tags(path)
        if not tags.get("title"):
            # File has no title tag — do the standard retag
            handle, title = tags_from_stem(path.stem)
            handle = artist or handle
            if not handle or not title:
                stats["skipped"] += 1
                continue
            if dry_run:
                reason = _embedded_tag_write_guard()
                if reason:
                    logger.warning("[dry-run] %s: %s", path.name, reason)
                    stats["failed"] += 1
                else:
                    logger.info("[dry-run] %s -> artist=%s title=%s", path.name, handle, title)
                    stats["tagged"] += 1
                continue
            result = tag_file(path, artist=handle, title=title)
            if result.tagged:
                stats["tagged"] += 1
            else:
                stats["failed"] += 1
                logger.warning("Could not tag %s: %s", path.name, result.reason)
        else:
            # File already has tags — check for lyrics backfill
            stats["skipped"] += 1
            if lyrics_sidecar and not dry_run:
                # Read lyrics from the tag (©lyr for MP4, lyrics for others)
                lyrics = tags.get("lyrics") or tags.get("\xa9lyr")
                if lyrics:
                    write_lyrics_sidecar(path, lyrics, lyrics_sidecar)
    return stats
