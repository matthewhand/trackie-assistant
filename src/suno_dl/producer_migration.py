"""Offline, journalled migration of filenames to explicitly supplied producers.

Planning is read-only. Before planning, applying or resuming, the caller MUST
establish that every other writer is stopped and stays stopped through recovery
and review. Snapshot checks can miss concurrent changes and are not a substitute
for excluding writers. Application and recovery hold the common advisory archive lock, but
direct tagging, retagging, metadata and sidecar helpers do not acquire it or
check the journal themselves. Older binaries and external applications may
also bypass it. The writers_stopped argument is an operator assertion and does
not discover or stop other processes. Cooperating downloader startup checks a
pending journal while holding the same authoritative root lock.

This module never uses a legacy sidecar artist or source handle as producer
evidence. Planning accepts an explicit None for diagnostics; apply requires a separately
verified producer value for every UUID.
Audio tag repair requires mutagen, ffmpeg and ffprobe, a single decodable audio
stream, a same-filesystem working copy, and sufficient free space. Linux inode
flags and fsx attributes must be queryable and default; this does not prove
preservation of attributes outside these interfaces. Retagging intentionally
changes mtime so Music Assistant can detect the update; atime, ctime and
birthtime are not preserved. Apply on Linux only after all writers are stopped
and while holding the archive lock.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .media_process import run as run_media_process
from .naming import build_filename
from .sidecar import _load_identity_json, read_sidecar
from .urls import UUID_RE

_MEDIA = {".m4a", ".mp3", ".mp4", ".wav"}
# Other audio types recognized by Trackie's retag inventory are not eligible
# for migration. Do not silently report an archive containing them complete.
_OTHER_AUDIO = {".aac", ".flac", ".m4b", ".ogg"}
_COVERS = {".cover.jpg", ".cover.jpeg", ".cover.png"}
_LYRICS = {".lrc", ".txt"}
# Known formats that can name archive files. This does not exhaust all
# external or unknown reference formats; operators must inspect those too.
_REFERENCE_FILES = {".m3u", ".m3u8", ".pls", ".cue", ".xspf", ".wpl", ".asx", ".wax", ".wvx"}
_JOURNAL = ".library/producer-rename-journal.json"
_MAX_JOURNAL = 32 * 1024 * 1024
_FS_EXTENT_FL = 0x00080000
# Linux fs.h defines FS_IOC_GETFLAGS as _IOR('f', 1, long). This value is
# valid on the explicitly supported 64-bit asm-generic ioctl ABIs below.
_FS_IOC_GETFLAGS = (2 << 30) | (ctypes.sizeof(ctypes.c_long) << 16) | (ord("f") << 8) | 1
# linux/fs.h: FS_IOC_FSGETXATTR = _IOR('X', 31, struct fsxattr), 28 bytes.
_FS_IOC_FSGETXATTR = (2 << 30) | (28 << 16) | (ord("X") << 8) | 31


class MigrationError(RuntimeError):
    """A safety precondition failed; leave the journal for inspection/resume."""


def journal_path(root: Path) -> Path:
    return Path(root) / _JOURNAL


def raise_if_migration_pending(root: Path) -> None:
    """Block a cooperating writer when an incomplete migration may exist.

    Call while holding the same archive-wide writer lock used for migration.
    This cannot protect against older writers that ignore that lock.
    """
    metadata = Path(root) / ".library"
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
        raise MigrationError("Unsafe library metadata directory")
    records = _read_completion_records(root, metadata) if metadata.exists() else {}
    journal = journal_path(root)
    if journal.exists() or journal.is_symlink():
        if _journal_completed(root, journal, records):
            return
        raise MigrationError("Pending producer migration must be resumed before writing")


_COMPLETION_PREFIX = "producer-completed-"
_MAX_COMPLETION = 4096


def _valid_migration_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _unique_completion_fields(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate completion field")
        result[key] = value
    return result


def _read_completion_records(root: Path, metadata: Path) -> dict[str, dict]:
    """Validate and durably sync every retained completion before trusting it."""
    records: dict[str, dict] = {}
    try:
        candidates = [
            path for path in metadata.iterdir() if path.name.startswith(_COMPLETION_PREFIX)
        ]
        for path in candidates:
            migration_id = path.name.removeprefix(_COMPLETION_PREFIX).removesuffix(".json")
            if not path.name.endswith(".json") or not _valid_migration_id(migration_id):
                raise MigrationError("Invalid migration completion filename")
            before = _regular(path)
            if before.st_size > _MAX_COMPLETION:
                raise MigrationError("Migration completion record is too large")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                    before.st_dev,
                    before.st_ino,
                    1,
                ):
                    raise MigrationError("Migration completion record changed")
                with os.fdopen(os.dup(fd), "rb") as stream:
                    raw = stream.read(_MAX_COMPLETION + 1)
                os.fsync(fd)
            finally:
                os.close(fd)
            data = json.loads(raw, object_pairs_hook=_unique_completion_fields)
            digest = data.get("journal_sha256") if isinstance(data, dict) else None
            root_info = root.stat()
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "id", "root_dev", "root_ino", "journal_sha256"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["root_dev"]) is not int
                or type(data["root_ino"]) is not int
                or data["id"] != migration_id
                or (data["root_dev"], data["root_ino"]) != (root_info.st_dev, root_info.st_ino)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise MigrationError("Invalid migration completion record")
            records[migration_id] = data
        if candidates:
            _fsync_dir(metadata)
    except (OSError, ValueError, TypeError) as exc:
        raise MigrationError("Cannot verify migration completion records") from exc
    return records


def _journal_completed(root: Path, journal: Path, records: dict[str, dict]) -> bool:
    if not records:
        return False
    try:
        info = _regular(journal)
        if info.st_size > _MAX_JOURNAL:
            raise MigrationError("Migration journal too large")
        raw = journal.read_bytes()
        plan = json.loads(raw, object_pairs_hook=_unique_completion_fields)
        if not isinstance(plan, dict):
            return False
        migration_id = plan.get("migration_id")
        record = (
            records.get(migration_id)
            if isinstance(migration_id, str) and _valid_migration_id(migration_id)
            else None
        )
        return bool(
            record
            and plan.get("version") in (6, 7, 8, 9, 10)
            and (plan.get("root_dev"), plan.get("root_ino"))
            == (root.stat().st_dev, root.stat().st_ino)
            and record["journal_sha256"] == _digest(raw)
        )
    except (OSError, ValueError, TypeError) as exc:
        raise MigrationError("Cannot verify migration journal completion") from exc


def _publish_completion(root: Path, journal: Path, plan: dict) -> None:
    """Publish the final journal digest; never delete a completion record."""
    migration_id = plan.get("migration_id")
    if not _valid_migration_id(migration_id):
        raise MigrationError("Invalid migration identity")
    metadata = journal.parent
    record_path = metadata / f"{_COMPLETION_PREFIX}{migration_id}.json"
    raw = journal.read_bytes()
    if len(raw) > _MAX_JOURNAL:
        raise MigrationError("Migration journal too large")
    try:
        stored = json.loads(raw, object_pairs_hook=_unique_completion_fields)
    except (ValueError, TypeError) as exc:
        raise MigrationError("Cannot verify final migration journal") from exc
    if stored != plan:
        raise MigrationError("Final migration journal changed")
    root_info = root.stat()
    data = {
        "version": 1,
        "id": migration_id,
        "root_dev": root_info.st_dev,
        "root_ino": root_info.st_ino,
        "journal_sha256": _digest(raw),
    }
    fd, name = tempfile.mkstemp(prefix=".producer-completion-", dir=metadata)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(data, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        _rename_no_replace(temporary, record_path)
        _fsync_dir(metadata)
    finally:
        temporary.unlink(missing_ok=True)


def _root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("Library root must be a real directory")
    return root.resolve(strict=True)


def _regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MigrationError(f"Cannot inspect {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MigrationError(f"Not a uniquely owned regular file: {path}")
    return info


def _inode_flags(path: Path) -> int:
    """Read Linux inode flags without following symlinks; never guess on failure."""
    if (
        not sys.platform.startswith("linux")
        or sys.byteorder != "little"
        or platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64"}
        or ctypes.sizeof(ctypes.c_long) != 8
    ):
        raise MigrationError("Inode flag verification is unsupported on this platform")
    import fcntl  # Linux-only; keep the module importable elsewhere.

    before = _regular(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, 1):
                raise MigrationError(f"File changed before inode flag inspection: {path}")
            result = bytearray(ctypes.sizeof(ctypes.c_long))
            fcntl.ioctl(fd, _FS_IOC_GETFLAGS, result, True)
            extra = bytearray(28)
            fcntl.ioctl(fd, _FS_IOC_FSGETXATTR, extra, True)
            xflags, extsize, _nextents, projid, cowextsize, padding = struct.unpack(
                "=IIIII8s", extra
            )
            # nextents describes allocation and normally differs after copying;
            # it is read-only. The remaining fields are persistent attributes.
            if xflags or extsize or projid or cowextsize or any(padding):
                raise MigrationError(f"Unsupported non-default inode flags or attributes: {path}")
            return int.from_bytes(result[:4], byteorder=sys.byteorder)
        finally:
            os.close(fd)
    except OSError as exc:
        raise MigrationError(f"Cannot verify inode flags: {path}") from exc


def _default_inode_flags(path: Path) -> int:
    flags = _inode_flags(path)
    # EXTENT is a kernel-managed representation bit on common Linux filesystems;
    # all other bits, including NODUMP, are refused rather than discarded.
    if flags & ~_FS_EXTENT_FL:
        raise MigrationError(f"Unsupported non-default inode flags: {path}")
    return flags


def _require_nonsparse(path: Path) -> None:
    """Refuse apparent holes; filesystems can still report holes as allocated data."""
    info = _regular(path)
    if not hasattr(info, "st_blocks"):
        raise MigrationError(f"Cannot verify source allocation: {path}")
    if info.st_size and info.st_blocks * 512 < info.st_size:
        raise MigrationError(f"Sparse or compressed source requires manual handling: {path}")
    if not info.st_size:
        return
    if not all(hasattr(os, name) for name in ("SEEK_HOLE", "O_NOFOLLOW")):
        raise MigrationError(f"Cannot verify source holes: {path}")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino, current.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
        ):
            raise MigrationError(f"Source changed while checking holes: {path}")
        first_hole = os.lseek(fd, 0, os.SEEK_HOLE)
        if first_hole < info.st_size:
            raise MigrationError(f"Sparse or compressed source requires manual handling: {path}")
        if os.fstat(fd).st_size != info.st_size:
            raise MigrationError(f"Source changed while checking holes: {path}")
    except OSError as exc:
        raise MigrationError(f"Cannot verify source holes: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _preflight_staging_space(root: Path, plan: dict) -> None:
    tag_entries = [tag for group in plan["groups"] for tag in group.get("tags", [])]
    largest = max((entry["size"] for entry in tag_entries), default=0)
    # One staged audio copy at a time, plus journal/metadata and allocation margin.
    required = largest + max(64 * 1024 * 1024, (largest + 9) // 10)
    metadata = root / ".library"
    location = metadata if metadata.is_dir() else root
    try:
        free = shutil.disk_usage(location).free
    except OSError as exc:
        raise MigrationError("Cannot verify staging free space") from exc
    if free < required:
        raise MigrationError(f"Insufficient staging space: need at least {required} bytes")


def _preflight_inode_flags(root: Path, plan: dict) -> None:
    """Record every file's expected flags before any migration mutation."""
    for group in plan["groups"]:
        sidecar = _path(root, group["old_sidecar"])
        _require_nonsparse(sidecar)
        group["sidecar_flags"] = _default_inode_flags(sidecar)
        for entry in group["members"]:
            source = _path(root, entry["old"])
            _require_nonsparse(source)
            entry["flags"] = _default_inode_flags(source)
        for entry in group.get("tags", []):
            source = _path(root, entry["source"])
            _require_nonsparse(source)
            entry["flags"] = _default_inode_flags(source)


def _require_matching_flags(path: Path, expected: object) -> None:
    if (
        not isinstance(expected, int)
        or isinstance(expected, bool)
        or expected not in (0, _FS_EXTENT_FL)
    ):
        raise MigrationError("Migration journal lacks verified inode flags")
    if _default_inode_flags(path) != expected:
        raise MigrationError(f"Inode flags changed or staging flags differ: {path}")


def _validate_journal_inode_flags(plan: dict) -> None:
    for group in plan["groups"]:
        values = [group.get("sidecar_flags")]
        values.extend(entry.get("flags") for entry in group.get("members", []))
        values.extend(entry.get("flags") for entry in group.get("tags", []))
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v not in (0, _FS_EXTENT_FL)
            for v in values
        ):
            raise MigrationError("Migration journal lacks verified inode flags")


def _require_no_xattrs(path: Path, kind: str = "Sidecar") -> None:
    _regular(path)
    if not hasattr(os, "listxattr"):
        raise MigrationError(f"Cannot verify {kind.lower()} extended attributes: {path}")
    try:
        if os.listxattr(path, follow_symlinks=False):
            raise MigrationError(f"{kind} extended attributes require manual handling: {path}")
    except (OSError, TypeError, NotImplementedError) as exc:
        raise MigrationError(f"Cannot verify {kind.lower()} extended attributes: {path}") from exc


def _require_original_sidecar(path: Path, group: dict) -> None:
    info = _regular(path)
    expected = (
        group.get("sidecar_dev"),
        group.get("sidecar_ino"),
        group.get("sidecar_size"),
        group.get("sidecar_mtime_ns"),
        group.get("sidecar_ctime_ns"),
        group.get("sidecar_uid"),
        group.get("sidecar_gid"),
        group.get("sidecar_mode"),
    )
    actual = (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_gid,
        stat.S_IMODE(info.st_mode),
    )
    if actual != expected:
        raise MigrationError(f"Sidecar identity or metadata changed: {path}")
    _require_matching_flags(path, group.get("sidecar_flags"))
    _require_no_xattrs(path)


def _require_installed_sidecar(path: Path, group: dict) -> None:
    info = _regular(path)
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
        group.get("sidecar_uid"),
        group.get("sidecar_gid"),
        group.get("sidecar_mode"),
    ):
        raise MigrationError(f"Installed sidecar ownership or permissions changed: {path}")
    _require_matching_flags(path, group.get("sidecar_flags"))
    _require_no_xattrs(path)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path(root: Path, rel: str) -> Path:
    if not isinstance(rel, str):
        raise MigrationError("Invalid journal path")
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts) or "\\" in rel:
        raise MigrationError("Unsafe journal path")
    current = root
    for part in p.parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise MigrationError("Unsafe journal directory")
    return current / p.name


def _unsafe_text(value: str) -> bool:
    return any(
        ord(char) < 32
        or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF
        or 0x202A <= ord(char) <= 0x202E
        or 0x2066 <= ord(char) <= 0x2069
        or ord(char) in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
        for char in value
    )


def _bounded_stem(stem: str, suffix: str = "") -> str:
    # Leave room for the longest member suffix under the 255-byte Linux limit.
    budget = 200 - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8")[:budget]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip(" .") + suffix
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    raise MigrationError("Empty filename after sanitizing")


def _read_artist(path: Path, extension: str | None = None) -> str | None:
    try:
        extension = extension or path.suffix.lower()
        if extension in {".m4a", ".mp4"}:
            from mutagen.mp4 import MP4

            audio = MP4(path)
            values = audio.tags.get("\xa9ART") if audio.tags is not None else None
        elif extension == ".wav":
            from mutagen.wave import WAVE

            wav = WAVE(path)
            frame = wav.tags.get("TPE1") if wav.tags is not None else None
            values = frame.text if frame is not None else None
        elif extension == ".mp3":
            from mutagen.mp3 import EasyMP3

            mp3 = EasyMP3(path)
            values = mp3.tags.get("artist") if mp3.tags is not None else None
        else:
            return None
        return values[0] if values and len(values) == 1 else None
    except Exception:  # Missing backend or unreadable file requires a repair attempt.
        return None


def _artist_matches(path: Path, artist: str) -> bool:
    return _read_artist(path) == artist


def _validate_artist_update(path: Path, extension: str, artist: str) -> None:
    try:
        if extension in {".m4a", ".mp4"}:
            from .lossless_mp4 import validate_mp4_update

            validate_mp4_update(path, {b"\xa9ART": artist})
        elif extension in {".mp3", ".wav"}:
            from .lossless_id3 import validate_artist_update

            validate_artist_update(path, extension, artist)
        else:
            raise ValueError("unsupported audio format")
    except ValueError as exc:
        raise MigrationError(f"Unsupported lossless artist update: {path.name}: {exc}") from exc


def _write_artist(path: Path, extension: str, artist: str) -> None:
    if extension in {".m4a", ".mp4"}:
        from .lossless_mp4 import write_isolated_mp4

        write_isolated_mp4(path, {b"\xa9ART": artist})
    elif extension in {".mp3", ".wav"}:
        from .lossless_id3 import write_isolated_artist

        write_isolated_artist(path, extension, artist)
    else:
        raise MigrationError("Unsupported audio format")


def _audio_signature(path: Path) -> str:
    probe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not probe or not ffmpeg:
        raise MigrationError("ffmpeg and ffprobe are required for audio verification")
    try:
        result = run_media_process(
            [probe, "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            check=True,
            timeout=60,
        )
        streams = json.loads(result.stdout)["streams"]
        if len(streams) != 1 or streams[0].get("codec_type") != "audio":
            raise MigrationError("Only a single audio stream is supported")
        result = run_media_process(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=600,
        )
        signature = result.stdout.decode("ascii").strip()
        if not signature.startswith("SHA256=") or len(signature) != 71:
            raise MigrationError("Invalid decoded audio signature")
        return signature
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, UnicodeError) as exc:
        raise MigrationError(f"Unable to verify decoded audio: {path}") from exc


def _metadata_fingerprint(path: Path, extension: str) -> tuple:
    """Conservatively verify inspected non-artist tags and reject artwork/chapters."""
    probe = shutil.which("ffprobe")
    if not probe:
        raise MigrationError("ffprobe is required for metadata verification")
    try:
        result = run_media_process(
            [
                probe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        details = json.loads(result.stdout)
        if details.get("chapters"):
            raise MigrationError("Audio chapters require manual handling")
        streams = details.get("streams", [])
        if len(streams) != 1 or streams[0].get("codec_type") != "audio":
            raise MigrationError("Artwork or multiple media streams require manual handling")

        def without_artist(tags: dict) -> tuple:
            return tuple(
                sorted((key, value) for key, value in tags.items() if key.casefold() != "artist")
            )

        probed = (
            without_artist(details.get("format", {}).get("tags", {})),
            without_artist(streams[0].get("tags", {})),
        )
        if extension in {".m4a", ".mp4"}:
            from mutagen.mp4 import MP4

            native = MP4(path).tags
            excluded = {"\xa9ART"}
        elif extension == ".wav":
            from mutagen.wave import WAVE

            native = WAVE(path).tags
            excluded = {"TPE1"}
        elif extension == ".mp3":
            from mutagen.mp3 import MP3

            native = MP3(path).tags
            excluded = {"TPE1"}
        else:
            raise MigrationError("Unsupported metadata format")
        values = []
        native_items: list[tuple[str, Any]] = list(native.items()) if native is not None else []
        for key, value in native_items:
            if key == "covr" or key.startswith(("APIC", "CHAP", "CTOC")):
                raise MigrationError("Artwork or chapters require manual handling")
            if key not in excluded:
                values.append((key, repr(value)))
        return probed, tuple(sorted(values))
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"Cannot verify audio metadata: {path}") from exc


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_metadata_tree(metadata: Path) -> None:
    """Refuse hidden audio and known filename references without following links."""

    def walk_error(_error: OSError) -> None:
        raise MigrationError("Cannot inventory the reserved metadata directory")

    for directory, dirs, files in os.walk(metadata, followlinks=False, onerror=walk_error):
        folder = Path(directory)
        for name in dirs + files:
            path = folder / name
            if path.is_symlink():
                raise MigrationError(f"Symlink in reserved metadata directory: {path}")
        for name in files:
            path = folder / name
            if path.suffix.lower() in (_MEDIA | _OTHER_AUDIO):
                raise MigrationError(f"Audio in reserved metadata directory: {path}")
            if path.suffix.lower() in _REFERENCE_FILES:
                raise MigrationError(f"Filename-referencing state in reserved metadata: {path}")


def _require_no_pending_publications(root: Path) -> None:
    metadata = root / ".library"
    if metadata.is_symlink():
        raise MigrationError("Unsafe library metadata directory")
    for name in ("verified-sync-intents", "download-intents", "tag-intents"):
        path = metadata / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise MigrationError("Unsafe publication intent directory")
        if path.exists() and any(path.iterdir()):
            raise MigrationError("Pending publication intents require recovery before migration")
    stage = root / ".suno-staging"
    if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
        raise MigrationError("Unsafe download staging directory")
    if stage.exists():
        for parent, dirs, files in os.walk(stage, followlinks=False):
            if files or any((Path(parent) / name).is_symlink() for name in dirs):
                raise MigrationError("Pending download staging requires review before migration")


def _walk(root: Path) -> list[Path]:
    result = []

    def walk_error(error: OSError) -> None:
        raise MigrationError("Cannot inventory the entire library") from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(directory)
        for name in dirs + files:
            p = folder / name
            if p.is_symlink():
                raise MigrationError(f"Symlink in library: {p}")
        if folder == root and ".library" in dirs:
            _check_metadata_tree(folder / ".library")
            dirs.remove(".library")
        for name in files:
            p = folder / name
            if name == ".suno-rotation.json" or p.suffix.lower() in _REFERENCE_FILES:
                raise MigrationError(f"Filename-referencing state requires manual handling: {p}")
            result.append(p)
    return result


def _snapshot_claimed(root: Path, observed: dict[Path, os.stat_result]) -> list[dict]:
    """Bind observed contents and metadata to the reviewed plan.

    This reads every claimed media, cover and sidecar in full. Metadata checks
    can miss concurrent changes, including equal-size rewrites and xattr races;
    all other writers must remain stopped throughout planning and cutover.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "listxattr"):
        raise MigrationError("Cannot verify reviewed file metadata")

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
            getattr(info, "st_blocks", -1),
        )

    entries = []
    for path in sorted(observed):
        before = observed[path]
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if identity(os.fstat(fd)) != identity(before):
                raise MigrationError(f"File changed while planning: {path}")
            flags = _default_inode_flags(path)
            xattrs = []
            for name in sorted(os.listxattr(fd)):
                value = os.getxattr(fd, name)
                xattrs.append([name, _digest(value)])
            digest = hashlib.sha256()
            with os.fdopen(os.dup(fd), "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if identity(os.fstat(fd)) != identity(before) or identity(path.lstat()) != identity(
                before
            ):
                raise MigrationError(f"File changed while planning: {path}")
            entries.append(
                {
                    "path": _relative(root, path),
                    "identity": list(identity(before)),
                    "inode_flags": flags,
                    "xattrs": xattrs,
                    "sha256": digest.hexdigest(),
                }
            )
        except (OSError, NotImplementedError) as exc:
            raise MigrationError(f"Cannot snapshot reviewed file: {path}") from exc
        finally:
            os.close(fd)
    return entries


def plan_migration(
    root: Path,
    producer_by_id: Mapping[str, str | None],
    *,
    creator_handle_by_id: Mapping[str, str] | None = None,
) -> dict:
    """Return a read-only plan. Each sidecar UUID must be in producer_by_id."""
    root = _root(root)
    _require_no_pending_publications(root)
    handles = dict(creator_handle_by_id or {})
    if set(handles) - set(producer_by_id) or any(
        not _valid_creator_handle(h) for h in handles.values()
    ):
        raise MigrationError("Invalid canonical producer handle mapping")
    if journal_path(root).exists() or journal_path(root).is_symlink():
        raise MigrationError("Pending migration journal must be resumed first")
    metadata = root / ".library"
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
        raise MigrationError("Unsafe metadata directory")
    intents = metadata / "download-intents"
    if intents.is_symlink() or (
        intents.exists() and (not intents.is_dir() or any(intents.iterdir()))
    ):
        raise MigrationError("Pending download intents require investigation")
    files = _walk(root)
    occupied = {_relative(root, p).casefold() for p in files}
    claimed: set[Path] = set()
    observed: dict[Path, os.stat_result] = {}
    ids: set[str] = set()
    groups = []
    for sidecar in sorted(p for p in files if p.suffix.lower() == ".json"):
        sidecar_info = _regular(sidecar)
        observed[sidecar] = sidecar_info
        data = read_sidecar(sidecar, strict_identity=True)
        if data is None:
            raise MigrationError(f"Unrecognized sidecar: {sidecar}")
        clip_id = data["id"].lower()
        if clip_id in ids:
            raise MigrationError(f"Duplicate UUID: {clip_id}")
        ids.add(clip_id)
        if clip_id not in producer_by_id:
            raise MigrationError(f"No verified producer decision for {clip_id}")
        artist = producer_by_id[clip_id]
        creator_handle = handles.get(clip_id)
        if artist is not None and (
            not isinstance(artist, str)
            or not artist.strip()
            or len(artist) > 256
            or _unsafe_text(artist)
            or len(artist.encode("utf-8")) > 512
        ):
            raise MigrationError(f"Invalid producer for {clip_id}")
        title = data.get("title")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 4096
            or _unsafe_text(title)
        ):
            raise MigrationError(f"Invalid title for {clip_id}")
        old_stem = sidecar.name[:-5]
        saved = data["saved_files"]
        if not saved or len(saved) != len({x for x in saved if isinstance(x, str)}):
            raise MigrationError(f"Invalid saved files for {clip_id}")
        members = []
        media_count = 0
        for name in saved:
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not name.startswith(old_stem + ".")
            ):
                raise MigrationError(f"Unsafe saved filename for {clip_id}")
            suffix = name[len(old_stem) :]
            if suffix.lower() not in _MEDIA | _COVERS:
                raise MigrationError(f"Unsupported saved filename for {clip_id}")
            path = sidecar.parent / name
            info = _regular(path)
            observed[path] = info
            if suffix.lower() in _MEDIA:
                media_count += 1
                if info.st_size <= 1024:
                    raise MigrationError(f"Media too small: {path}")
            if path in claimed:
                raise MigrationError(f"File claimed twice: {path}")
            claimed.add(path)
            members.append((path, suffix, info))
        if not media_count:
            raise MigrationError(f"No valid media for {clip_id}")
        # A matching stem alone does not prove ownership of lyrics or notes.
        # Such files are refused during the final inventory check below.
        claimed.add(sidecar)
        stem = _bounded_stem(
            build_filename(
                "{artist}_{title}", title=title, handle="", clip_id=clip_id, artist=artist
            )
        )

        def targets(candidate: str) -> list[Path]:
            return [sidecar.parent / (candidate + ".json")] + [  # noqa: B023
                sidecar.parent / (candidate + suffix)  # noqa: B023
                for _, suffix, _ in members  # noqa: B023
            ]

        old_paths = {sidecar, *(p for p, _, _ in members)}

        def conflicts(candidate: str) -> bool:
            ts = targets(candidate)
            keys = [_relative(root, p).casefold() for p in ts]
            return len(keys) != len(set(keys)) or any(
                k in occupied and p not in old_paths  # noqa: B023
                for k, p in zip(keys, ts, strict=True)  # noqa: B023
            )

        if conflicts(stem):
            stem = _bounded_stem(stem, "_" + clip_id)
        if conflicts(stem):
            raise MigrationError(f"Filename collision for {clip_id}")
        destinations = targets(stem)
        for p in destinations:
            occupied.add(_relative(root, p).casefold())
        new_sidecar = destinations[0]
        records = []
        tag_records = []
        reviewed_members = []
        replacements = {}
        for index, ((old, suffix, info), new) in enumerate(
            zip(members, destinations[1:], strict=True)
        ):
            replacements[old.name] = new.name
            reviewed_members.append({"old": _relative(root, old), "new": _relative(root, new)})
            if suffix.lower() in _MEDIA and artist is not None and not _artist_matches(old, artist):
                tag_records.append(
                    {
                        "path": _relative(root, new),
                        "source": _relative(root, old),
                        "extension": suffix.lower(),
                        "artist": artist,
                        "stage_name": f"{clip_id}-{index}.part",
                        "dev": info.st_dev,
                        "ino": info.st_ino,
                        "size": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                        "ma_mtime": int(info.st_mtime),
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                )
            if old != new:
                records.append(
                    {
                        "old": _relative(root, old),
                        "new": _relative(root, new),
                        "dev": info.st_dev,
                        "ino": info.st_ino,
                        "size": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                    }
                )
        if (
            new_sidecar != sidecar
            or (artist is not None and data.get("artist") != artist)
            or data.get("creator_handle") != creator_handle
            or tag_records
        ):
            original = sidecar.read_bytes()
            new_data = dict(data)
            if artist is not None:
                new_data["artist"] = artist
            # Never retain an unreviewed legacy producer-profile claim.
            new_data["creator_handle"] = creator_handle
            new_data["saved_files"] = [replacements[n] for n in saved]
            new_bytes = (json.dumps(new_data, ensure_ascii=False, indent=2) + "\n").encode()
            groups.append(
                {
                    "id": clip_id,
                    "producer_decision": artist,
                    "creator_handle_decision": creator_handle,
                    "old_sidecar": _relative(root, sidecar),
                    "new_sidecar": _relative(root, new_sidecar),
                    "old_hash": _digest(original),
                    "new_hash": _digest(new_bytes),
                    "new_content": new_bytes.decode(),
                    "sidecar_uid": sidecar_info.st_uid,
                    "sidecar_gid": sidecar_info.st_gid,
                    "sidecar_mode": stat.S_IMODE(sidecar_info.st_mode),
                    "sidecar_dev": sidecar_info.st_dev,
                    "sidecar_ino": sidecar_info.st_ino,
                    "sidecar_size": sidecar_info.st_size,
                    "sidecar_mtime_ns": sidecar_info.st_mtime_ns,
                    "sidecar_ctime_ns": sidecar_info.st_ctime_ns,
                    "members": records,
                    "reviewed_members": reviewed_members,
                    "tags": tag_records,
                }
            )
        elif records:
            raise MigrationError("Inconsistent filename group")
    for path in files:
        if path.suffix.lower() in (_MEDIA | _OTHER_AUDIO) and path not in claimed:
            raise MigrationError(f"Unowned media or unsupported audio: {path}")
        if any(path.name.casefold().endswith(s) for s in _COVERS | _LYRICS) and path not in claimed:
            raise MigrationError(f"Unowned cover or lyrics: {path}")
    info = root.stat()
    return {
        "version": 10,
        "tag_writer": "lossless-artist-v1",
        "root_dev": info.st_dev,
        "root_ino": info.st_ino,
        "groups": groups,
        "reviewed_files": _snapshot_claimed(root, observed),
    }


def _preflight_resumed_inventory(root: Path, plan: dict) -> None:
    """Reject newly introduced unreviewed media or known filename references."""
    try:
        approved = {entry["path"] for entry in plan["reviewed_files"]}
        for group in plan["groups"]:
            approved.add(group["new_sidecar"])
            approved.update(member["new"] for member in group["reviewed_members"])
    except (KeyError, TypeError) as exc:
        raise MigrationError("Invalid reviewed inventory") from exc
    for path in _walk(root):
        suffix = path.suffix.lower()
        relevant = suffix in (_MEDIA | _OTHER_AUDIO | {".json"}) or any(
            path.name.casefold().endswith(ext) for ext in _COVERS | _LYRICS
        )
        if relevant and _relative(root, path) not in approved:
            raise MigrationError(f"Unreviewed archive member requires inspection: {path}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_no_replace(old: Path, new: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fn = libc.renameat2
    except AttributeError as exc:
        raise MigrationError("Atomic no-replace rename is unavailable") from exc
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(-100, os.fsencode(old), -100, os.fsencode(new), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(new))


def _linux() -> None:
    if not sys.platform.startswith("linux"):
        raise MigrationError("Migration application currently requires Linux")


def _save_journal(journal: Path, plan: dict) -> None:
    encoded = (json.dumps(plan, ensure_ascii=False) + "\n").encode()
    if len(encoded) > _MAX_JOURNAL:
        raise MigrationError("Migration journal too large")
    _regular(journal)
    fd, temp_name = tempfile.mkstemp(prefix=".producer-journal-", dir=journal.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, journal)
        _fsync_dir(journal.parent)
    finally:
        temp.unlink(missing_ok=True)


def _identity_matches(st: os.stat_result, record: dict) -> bool:
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_uid,
        st.st_gid,
        stat.S_IMODE(st.st_mode),
    ) == (
        record["dev"],
        record["ino"],
        record["size"],
        record["mtime_ns"],
        record["uid"],
        record["gid"],
        record["mode"],
    )


def _staging_dir(root: Path, plan: dict) -> Path:
    details = plan["staging"]
    path = _path(root, details["path"])
    try:
        st = path.lstat()
    except OSError as exc:
        raise MigrationError("Migration staging directory is missing") from exc
    if (
        not stat.S_ISDIR(st.st_mode)
        or stat.S_IMODE(st.st_mode) != 0o700
        or (st.st_dev, st.st_ino, st.st_uid) != (details["dev"], details["ino"], details["uid"])
    ):
        raise MigrationError("Unsafe migration staging directory")
    return path


def _sync_recovered_staging(root: Path, plan: dict) -> None:
    """Durably confirm the staging directory or its already-removed state."""
    stage = _path(root, plan["staging"]["path"])
    if stage.exists() or stage.is_symlink():
        _fsync_dir(_staging_dir(root, plan))
    else:
        _fsync_dir(stage.parent)


def _require_reviewed_file(root: Path, path: Path, expected: dict, *, moved: bool) -> None:
    """Verify original content and metadata; rename alone may change ctime."""
    try:
        identity = expected["identity"]
        if (
            not isinstance(identity, list)
            or len(identity) != 10
            or any(type(value) is not int for value in identity)
            or not isinstance(expected["sha256"], str)
            or not isinstance(expected["xattrs"], list)
            or type(expected["inode_flags"]) is not int
        ):
            raise ValueError("invalid snapshot")
        actual = _snapshot_claimed(root, {path: _regular(path)})[0]
        actual_identity = actual["identity"]
        # Renaming preserves identity/content but changes ctime on Linux.
        indices = range(10) if not moved else (0, 1, 2, 3, 5, 6, 7, 8, 9)
        if (
            any(actual_identity[i] != identity[i] for i in indices)
            or actual["inode_flags"] != expected["inode_flags"]
            or actual["xattrs"] != expected["xattrs"]
            or actual["sha256"] != expected["sha256"]
        ):
            raise MigrationError(f"Reviewed file changed: {path}")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise MigrationError("Invalid reviewed file snapshot") from exc


def _require_prepared_audio_for_preflight(path: Path, tag: dict) -> None:
    """Check installed retagged audio without mutating or advancing recovery."""
    prepared = tag.get("prepared")
    if not isinstance(prepared, dict):
        raise MigrationError(f"Unowned reviewed media destination: {path}")
    info = _regular(path)
    try:
        if (
            (info.st_dev, info.st_ino, info.st_size)
            != (prepared["dev"], prepared["ino"], prepared["size"])
            or (info.st_mtime_ns, int(info.st_mtime)) != _require_prepared_mtime(tag, prepared)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            != (tag["uid"], tag["gid"], tag["mode"])
            or _file_hash(path) != prepared["hash"]
        ):
            raise MigrationError(f"Repaired audio changed: {path}")
        _require_matching_flags(path, tag["flags"])
        _require_no_xattrs(path, "Audio")
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationError("Invalid prepared audio snapshot") from exc


def _preflight_reviewed_files(root: Path, plan: dict) -> None:
    """Validate every reviewed original or journal-owned retag before moving."""
    snapshots = plan.get("reviewed_files")
    if not isinstance(snapshots, list):
        raise MigrationError("Migration journal lacks reviewed file snapshots")
    entries: dict[str, dict] = {}
    for snapshot in snapshots:
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"path", "identity", "inode_flags", "xattrs", "sha256"}
            or not isinstance(snapshot.get("path"), str)
            or not isinstance(snapshot.get("identity"), list)
            or len(snapshot["identity"]) != 10
            or any(type(value) is not int for value in snapshot["identity"])
            or type(snapshot.get("inode_flags")) is not int
            or snapshot["inode_flags"] not in (0, _FS_EXTENT_FL)
            or not isinstance(snapshot.get("xattrs"), list)
            or not _valid_review_digest(snapshot.get("sha256"))
        ):
            raise MigrationError("Invalid reviewed file snapshot")
        relative = snapshot["path"]
        _path(root, relative)
        if relative in entries:
            raise MigrationError("Duplicate reviewed file snapshot")
        entries[relative] = snapshot
    mapped: dict[str, tuple[str, dict | None]] = {}
    changed_sidecars = set()
    for group in plan["groups"]:
        old_sidecar = _path(root, group["old_sidecar"])
        new_sidecar = _path(root, group["new_sidecar"])
        if old_sidecar.parent != new_sidecar.parent or group["old_sidecar"] not in entries:
            raise MigrationError("Sidecar lacks a reviewed original snapshot")
        changed_sidecars.add(group["old_sidecar"])
        if old_sidecar.exists() or old_sidecar.is_symlink():
            info = _regular(old_sidecar)
            if (info.st_dev, info.st_ino) == (group.get("sidecar_dev"), group.get("sidecar_ino")):
                _require_original_sidecar(old_sidecar, group)
                if _file_hash(old_sidecar) != group["old_hash"]:
                    raise MigrationError(f"Sidecar changed: {old_sidecar}")
            elif old_sidecar == new_sidecar:
                _require_prepared_sidecar(old_sidecar, group)
            else:
                raise MigrationError(f"Sidecar changed: {old_sidecar}")
        elif not new_sidecar.exists():
            raise MigrationError("Both sidecar paths are missing")
        if old_sidecar != new_sidecar and (new_sidecar.exists() or new_sidecar.is_symlink()):
            _require_prepared_sidecar(new_sidecar, group)
        members = group.get("reviewed_members")
        if not isinstance(members, list):
            raise MigrationError("Migration journal lacks reviewed member mappings")
        tags = group.get("tags", [])
        moves = group.get("members")
        if not isinstance(tags, list) or not isinstance(moves, list):
            raise MigrationError("Invalid migration member or tag entries")
        group_pairs: set[tuple[str, str]] = set()
        for member in members:
            if not isinstance(member, dict) or set(member) != {"old", "new"}:
                raise MigrationError("Invalid reviewed member mapping")
            old, new = member["old"], member["new"]
            if not isinstance(old, str) or not isinstance(new, str) or old in mapped:
                raise MigrationError("Invalid reviewed member mapping")
            if old not in entries:
                raise MigrationError("Reviewed member lacks original snapshot")
            old_path, new_path = _path(root, old), _path(root, new)
            sidecar_parent = _path(root, group["old_sidecar"]).parent
            if old_path.parent != sidecar_parent or new_path.parent != sidecar_parent:
                raise MigrationError("Reviewed member leaves its sidecar directory")
            group_pairs.add((old, new))
            matches = [
                tag
                for tag in tags
                if isinstance(tag, dict) and tag.get("source") == old and tag.get("path") == new
            ]
            if len(matches) > 1:
                raise MigrationError("Duplicate reviewed media tags")
            mapped[old] = (new, matches[0] if matches else None)
        move_pairs = []
        for move in moves:
            if (
                not isinstance(move, dict)
                or not isinstance(move.get("old"), str)
                or not isinstance(move.get("new"), str)
            ):
                raise MigrationError("Invalid migration member")
            move_pairs.append((move["old"], move["new"]))
        if len(move_pairs) != len(set(move_pairs)) or set(move_pairs) != {
            pair for pair in group_pairs if pair[0] != pair[1]
        }:
            raise MigrationError("Reviewed members disagree with planned moves")
        for tag in tags:
            if (
                not isinstance(tag, dict)
                or not isinstance(tag.get("source"), str)
                or not isinstance(tag.get("path"), str)
                or (tag["source"], tag["path"]) not in group_pairs
            ):
                raise MigrationError("Reviewed members disagree with planned tags")
    for relative, expected in entries.items():
        # Changed sidecars have separate original/prepared inode and hash checks.
        if relative in changed_sidecars:
            continue
        old = _path(root, relative)
        if relative not in mapped:
            _require_reviewed_file(root, old, expected, moved=False)
            continue
        new_relative, tag = mapped[relative]
        new = _path(root, new_relative)
        if old == new:
            path = old
            moved = False
        else:
            old_exists = old.exists() or old.is_symlink()
            new_exists = new.exists() or new.is_symlink()
            if old_exists == new_exists:
                raise MigrationError(f"Reviewed member move conflict: {old}")
            path = old if old_exists else new
            moved = new_exists
        info = _regular(path)
        original_identity = expected.get("identity")
        if (
            isinstance(original_identity, list)
            and len(original_identity) == 10
            and (info.st_dev, info.st_ino) == (original_identity[0], original_identity[1])
        ):
            _require_reviewed_file(root, path, expected, moved=moved)
        elif tag is not None:
            _require_prepared_audio_for_preflight(path, tag)
        else:
            raise MigrationError(f"Reviewed file identity changed: {path}")


def _require_prepared_sidecar(path: Path, group: dict) -> None:
    prepared = group.get("sidecar_prepared")
    if not isinstance(prepared, dict):
        raise MigrationError(f"Unowned sidecar destination: {path}")
    info = _regular(path)
    if (info.st_dev, info.st_ino, info.st_size) != (
        prepared.get("dev"),
        prepared.get("ino"),
        prepared.get("size"),
    ):
        raise MigrationError(f"Unowned sidecar destination: {path}")
    _require_installed_sidecar(path, group)
    if _file_hash(path) != group["new_hash"]:
        raise MigrationError(f"Prepared sidecar changed: {path}")


_NANOSECONDS = 1_000_000_000


def _require_prepared_mtime(entry: dict, prepared: dict) -> tuple[int, int]:
    original = entry.get("ma_mtime")
    prepared_ns = prepared.get("mtime_ns")
    prepared_ma = prepared.get("ma_mtime")
    if (
        not isinstance(original, int)
        or isinstance(original, bool)
        or not isinstance(prepared_ns, int)
        or isinstance(prepared_ns, bool)
        or not isinstance(prepared_ma, int)
        or isinstance(prepared_ma, bool)
        or original == prepared_ma
    ):
        raise MigrationError("Prepared audio lacks a distinct Music Assistant mtime")
    return prepared_ns, prepared_ma


def _require_supported_tag_update(entry: dict) -> None:
    """Refuse retags until opaque container metadata is preserved safely."""
    extension = entry.get("extension")
    source = entry.get("source")
    target = entry.get("path")
    if (
        not isinstance(extension, str)
        or extension not in _MEDIA
        or not isinstance(source, str)
        or not isinstance(target, str)
        or Path(source).suffix.lower() != extension
        or Path(target).suffix.lower() != extension
    ):
        raise MigrationError("Invalid migration tag extension")


def _require_supported_tag_updates(plan: dict) -> None:
    try:
        if plan.get("tag_writer") != "lossless-artist-v1":
            raise MigrationError("Unrecognized migration tag writer")
        for group in plan["groups"]:
            for entry in group["tags"]:
                _require_supported_tag_update(entry)
    except (KeyError, TypeError, AttributeError) as exc:
        raise MigrationError("Invalid migration tag entries") from exc


def _retag_one(root: Path, journal: Path, plan: dict, entry: dict) -> None:
    _require_supported_tag_update(entry)
    target = _path(root, entry["path"])
    st = _regular(target)
    _require_matching_flags(target, entry["flags"])
    prepared = entry.get("prepared")
    prepared_mtime = _require_prepared_mtime(entry, prepared) if prepared else None
    if prepared and (st.st_dev, st.st_ino, st.st_size) == (
        prepared["dev"],
        prepared["ino"],
        prepared["size"],
    ):
        if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (
            entry["uid"],
            entry["gid"],
            entry["mode"],
        ):
            raise MigrationError(f"Repaired audio ownership or permissions changed: {target}")
        _require_no_xattrs(target, "Audio")
        if (st.st_mtime_ns, int(st.st_mtime)) != prepared_mtime or _file_hash(target) != prepared[
            "hash"
        ]:
            raise MigrationError(f"Repaired audio changed: {target}")
        _fsync_dir(target.parent)
        _sync_recovered_staging(root, plan)
        return
    if not _identity_matches(st, entry):
        raise MigrationError(f"Audio changed: {target}")
    stage_dir = _staging_dir(root, plan)
    if os.stat(target.parent).st_dev != os.stat(stage_dir).st_dev:
        raise MigrationError("Audio and staging directory must share a filesystem")
    stage_name = entry["stage_name"]
    if (
        not isinstance(stage_name, str)
        or Path(stage_name).name != stage_name
        or not stage_name.endswith(".part")
    ):
        raise MigrationError("Unsafe audio staging name")
    stage = stage_dir / stage_name
    if prepared:
        if stage.exists() or stage.is_symlink():
            stage_stat = _regular(stage)
            if (
                (stage_stat.st_dev, stage_stat.st_ino, stage_stat.st_size)
                != (
                    prepared["dev"],
                    prepared["ino"],
                    prepared["size"],
                )
                or (stage_stat.st_mtime_ns, int(stage_stat.st_mtime)) != prepared_mtime
                or _file_hash(stage) != prepared["hash"]
            ):
                raise MigrationError("Prepared audio was changed")
        else:
            # A crash before directory sync can leave the original in place.
            del entry["prepared"]
            _save_journal(journal, plan)
            prepared = None
    if not prepared:
        # A crash before the prepared record was durable leaves ownership
        # unproven. Never delete or adopt that file automatically.
        if stage.exists() or stage.is_symlink():
            raise MigrationError("Unjournalled audio stage requires manual review")
        signature = _audio_signature(target)
        metadata_signature = _metadata_fingerprint(target, entry["extension"])
        fd = os.open(
            stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            with os.fdopen(fd, "wb") as output, target.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _file_hash(stage) != _file_hash(target) or not _identity_matches(
                _regular(target), entry
            ):
                raise MigrationError("Source changed while preparing audio")
            _write_artist(stage, entry["extension"], entry["artist"])
            if (
                _read_artist(stage, entry["extension"]) != entry["artist"]
                or _audio_signature(stage) != signature
                or _metadata_fingerprint(stage, entry["extension"]) != metadata_signature
            ):
                raise MigrationError("Tagged audio or metadata failed verification")
            with stage.open("rb") as staged_file:
                os.fchown(staged_file.fileno(), st.st_uid, st.st_gid)
                os.fchmod(staged_file.fileno(), stat.S_IMODE(st.st_mode))
                staged_stat = os.fstat(staged_file.fileno())
                desired_ns = time.time_ns()
                os.utime(staged_file.fileno(), ns=(staged_stat.st_atime_ns, desired_ns))
                staged_stat = os.fstat(staged_file.fileno())
                if int(staged_stat.st_mtime) == entry["ma_mtime"]:
                    # Move to the previous second instead of creating a future mtime.
                    os.utime(
                        staged_file.fileno(),
                        ns=(staged_stat.st_atime_ns, desired_ns - 2 * _NANOSECONDS),
                    )
                os.fsync(staged_file.fileno())
            if os.listxattr(stage, follow_symlinks=False):
                raise MigrationError("Staged audio has unsupported extended attributes")
            _require_matching_flags(stage, entry["flags"])
            _fsync_dir(stage_dir)
            staged_stat = _regular(stage)
            entry["prepared"] = {
                "dev": staged_stat.st_dev,
                "ino": staged_stat.st_ino,
                "size": staged_stat.st_size,
                "mtime_ns": staged_stat.st_mtime_ns,
                "ma_mtime": int(staged_stat.st_mtime),
                "hash": _file_hash(stage),
            }
            _require_prepared_mtime(entry, entry["prepared"])
            _save_journal(journal, plan)
        except Exception:
            # If journal persistence failed, do not remove an already recorded stage.
            raise
    if not _identity_matches(_regular(target), entry):
        raise MigrationError(f"Audio changed before installation: {target}")
    staged_info = _regular(stage)
    _require_matching_flags(stage, entry["flags"])
    if (staged_info.st_uid, staged_info.st_gid, stat.S_IMODE(staged_info.st_mode)) != (
        entry["uid"],
        entry["gid"],
        entry["mode"],
    ):
        raise MigrationError("Staged audio ownership or mode changed")
    if os.listxattr(target, follow_symlinks=False) or os.listxattr(stage, follow_symlinks=False):
        raise MigrationError("Audio extended attributes changed")
    os.replace(stage, target)
    _fsync_dir(target.parent)
    _fsync_dir(stage_dir)


def _require_sync_context() -> None:
    """Avoid blocking an event loop that may own the archive lock."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise MigrationError("Run migration in a dedicated synchronous process, not an event loop")


def review_plan_digest(plan: dict) -> str:
    """Return a canonical digest for an independently reviewed read-only plan."""
    try:
        raw = json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MigrationError("Cannot fingerprint migration plan") from exc
    return _digest(raw)


def _valid_review_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _resolved_producer(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or _unsafe_text(value)
        or len(value.encode("utf-8")) > 512
    ):
        return False
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    label = " ".join(normalized.replace("_", " ").replace("-", " ").split())
    compact = "".join(char for char in label if char.isalnum())
    return bool(compact) and compact not in {
        "unknown",
        "unknownartist",
        "unknownproducer",
        "unresolved",
        "unresolvedartist",
        "unresolvedproducer",
        "na",
        "none",
        "null",
        "tbd",
        "unverified",
        "unverifiedartist",
        "unverifiedproducer",
        "notverified",
        "notverifiedartist",
        "notverifiedproducer",
        "notavailable",
        "notapplicable",
        "notknown",
    }


def _valid_creator_handle(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 255
        and value.isascii()
        and all(char.isalnum() or char in "_-" for char in value)
    )


def _require_resolved_journal_producers(plan: dict) -> None:
    seen: set[str] = set()
    for group in plan["groups"]:
        if not isinstance(group, dict):
            raise MigrationError("Invalid migration producer decision")
        clip_id = group.get("id")
        artist = group.get("producer_decision")
        creator_handle = group.get("creator_handle_decision")
        if (
            not isinstance(clip_id, str)
            or not UUID_RE.fullmatch(clip_id)
            or clip_id in seen
            or not _resolved_producer(artist)
        ):
            raise MigrationError("Unresolved migration producer requires independent review")
        seen.add(clip_id)
        try:
            new_data = _load_identity_json(group["new_content"])
            tags = group.get("tags", [])
            if (
                not isinstance(new_data, dict)
                or not isinstance(new_data.get("id"), str)
                or new_data["id"].lower() != clip_id
                or new_data.get("artist") != artist
                or "creator_handle_decision" not in group
                or (creator_handle is not None and not _valid_creator_handle(creator_handle))
                or new_data.get("creator_handle") != creator_handle
                or not isinstance(tags, list)
                or any(not isinstance(tag, dict) or tag.get("artist") != artist for tag in tags)
            ):
                raise ValueError("decision mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise MigrationError("Migration producer decision does not match journal") from exc


def apply_migration(
    root: Path,
    producer_by_id: Mapping[str, str | None],
    *,
    creator_handle_by_id: Mapping[str, str] | None = None,
    reviewed_creator_handles: Mapping[str, str] | None = None,
    writers_stopped: bool = False,
    producer_mapping_reviewed: bool = False,
    reviewed_producers: Mapping[str, str | None] | None = None,
    reviewed_plan_sha256: str | None = None,
) -> int:
    """Apply only an independently reviewed plan under the archive lock.

    The assertion and digests bind a caller's review to specific decisions and
    paths; they do not prove who reviewed them or where producer data came from.
    """
    if writers_stopped is not True:
        raise MigrationError("All library writers must be stopped first")
    if producer_mapping_reviewed is not True or reviewed_producers is None:
        raise MigrationError("Independent producer mapping review is required")
    if not _valid_review_digest(reviewed_plan_sha256):
        raise MigrationError("A reviewed migration plan digest is required")
    try:
        requested = dict(producer_by_id)
        reviewed = dict(reviewed_producers)
    except (TypeError, ValueError) as exc:
        raise MigrationError("Invalid reviewed producer mapping") from exc
    if requested != reviewed:
        raise MigrationError("Reviewed producer mapping does not match requested decisions")
    if any(not _resolved_producer(value) for value in requested.values()):
        raise MigrationError("Unresolved producer decisions cannot be applied")
    if creator_handle_by_id is not None:
        if reviewed_creator_handles is None or dict(creator_handle_by_id) != dict(
            reviewed_creator_handles
        ):
            raise MigrationError("Independent canonical handle mapping review is required")
    elif reviewed_creator_handles is not None:
        raise MigrationError("Unexpected canonical handle review without requested handles")
    _linux()
    _require_sync_context()
    root = _root(root)
    from .archive_lock import _exclusive_archive_sync

    with _exclusive_archive_sync(root):
        return _apply_migration_locked(
            root,
            requested,
            creator_handle_by_id=creator_handle_by_id,
            writers_stopped=True,
            reviewed_plan_sha256=reviewed_plan_sha256,
        )


def resume_migration(
    root: Path,
    *,
    writers_stopped: bool = False,
    producer_mapping_reviewed: bool = False,
    reviewed_journal_sha256: str | None = None,
) -> int:
    """Resume only after independent review of the exact pending journal.

    Review must establish producer provenance separately. Matching a hash
    binds the assertion to journal bytes; it does not validate their source.
    """
    if writers_stopped is not True:
        raise MigrationError("All library writers must be stopped first")
    if producer_mapping_reviewed is not True:
        raise MigrationError("Independent producer mapping review is required")
    if not _valid_review_digest(reviewed_journal_sha256):
        raise MigrationError("A reviewed migration journal digest is required")
    _linux()
    _require_sync_context()
    root = _root(root)
    from .archive_lock import _exclusive_archive_sync

    with _exclusive_archive_sync(root):
        journal = journal_path(root)
        info = _regular(journal)
        if info.st_size > _MAX_JOURNAL or _file_hash(journal) != reviewed_journal_sha256:
            raise MigrationError("Pending migration journal differs from reviewed journal")
        return _resume_migration_locked(root, writers_stopped=True)


def _apply_migration_locked(
    root: Path,
    producer_by_id: Mapping[str, str | None],
    *,
    creator_handle_by_id: Mapping[str, str] | None = None,
    writers_stopped: bool = False,
    reviewed_plan_sha256: str | None = None,
) -> int:
    """Apply the already reviewed plan while holding the shared writer lock."""
    if not writers_stopped:
        raise MigrationError("All library writers must be stopped first")
    _linux()
    root = _root(root)
    plan = plan_migration(root, producer_by_id, creator_handle_by_id=creator_handle_by_id)
    if review_plan_digest(plan) != reviewed_plan_sha256:
        raise MigrationError("Migration plan differs from the reviewed plan")
    if not plan["groups"]:
        return 0
    _require_supported_tag_updates(plan)
    plan["migration_id"] = str(uuid.uuid4())
    _preflight_inode_flags(root, plan)
    _preflight_staging_space(root, plan)
    for group in plan["groups"]:
        for tag in group["tags"]:
            _validate_artist_update(_path(root, tag["source"]), tag["extension"], tag["artist"])
    metadata = root / ".library"
    if not metadata.exists():
        metadata.mkdir(mode=0o700)
        _fsync_dir(root)
    if plan["migration_id"] in _read_completion_records(root, metadata):
        raise MigrationError("Migration completion identity already exists")
    if not hasattr(os, "listxattr"):
        raise MigrationError("Cannot verify sidecar extended attributes")
    for group in plan["groups"]:
        sidecar = _path(root, group["old_sidecar"])
        try:
            if os.listxattr(sidecar, follow_symlinks=False):
                raise MigrationError(
                    f"Sidecar extended attributes require manual handling: {sidecar}"
                )
        except OSError as exc:
            raise MigrationError(f"Cannot verify sidecar extended attributes: {sidecar}") from exc
    tag_entries = [tag for group in plan["groups"] for tag in group["tags"]]
    if tag_entries:
        if (
            importlib.util.find_spec("mutagen") is None
            or not shutil.which("ffmpeg")
            or not shutil.which("ffprobe")
        ):
            raise MigrationError("Audio tagging requires mutagen, ffmpeg and ffprobe")
        for tag in tag_entries:
            if _path(root, tag["path"]).parent.stat().st_dev != metadata.stat().st_dev:
                raise MigrationError("Audio and staging directory must share a filesystem")
            source = _path(root, tag["source"])
            if not _identity_matches(_regular(source), tag):
                raise MigrationError(f"Audio changed during planning: {source}")
            if not hasattr(os, "listxattr"):
                raise MigrationError("Cannot verify audio extended attributes")
            try:
                if os.listxattr(source, follow_symlinks=False):
                    raise MigrationError(
                        f"Audio extended attributes require manual handling: {source}"
                    )
            except OSError as exc:
                raise MigrationError(f"Cannot verify audio extended attributes: {source}") from exc
            _audio_signature(source)
            _metadata_fingerprint(source, tag["extension"])
    for group in plan["groups"]:
        if _path(root, group["old_sidecar"]).parent.stat().st_dev != metadata.stat().st_dev:
            raise MigrationError("Sidecar and staging directory must share a filesystem")
    stage = Path(tempfile.mkdtemp(prefix=".producer-tag-", dir=metadata))
    info = stage.stat()
    plan["staging"] = {
        "path": _relative(root, stage),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "uid": info.st_uid,
    }
    _fsync_dir(metadata)
    journal = journal_path(root)
    encoded = (json.dumps(plan, ensure_ascii=False) + "\n").encode()
    if len(encoded) > _MAX_JOURNAL:
        raise MigrationError("Migration journal too large")
    fd = os.open(
        journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_dir(metadata)
    return _resume_migration_locked(root, writers_stopped=True)


def _resume_migration_locked(root: Path, *, writers_stopped: bool = False) -> int:
    """Resume a durable journal; conflicts stop without overwriting anything."""
    if not writers_stopped:
        raise MigrationError("All library writers must be stopped first")
    _linux()
    root = _root(root)
    _require_no_pending_publications(root)
    metadata = root / ".library"
    if metadata.is_symlink() or not metadata.is_dir():
        raise MigrationError("Unsafe metadata directory")
    journal = journal_path(root)
    records = _read_completion_records(root, metadata)
    if _journal_completed(root, journal, records):
        journal.unlink()
        _fsync_dir(metadata)
        return 0
    info = _regular(journal)
    if info.st_size > _MAX_JOURNAL:
        raise MigrationError("Migration journal too large")
    try:
        plan = json.loads(
            journal.read_text(encoding="utf-8"), object_pairs_hook=_unique_completion_fields
        )
        if isinstance(plan, dict) and plan.get("version") in (6, 7, 8, 9):
            raise MigrationError("Legacy migration journal requires manual review")
        if (
            plan["version"] != 10
            or not _valid_migration_id(plan.get("migration_id"))
            or (plan["root_dev"], plan["root_ino"]) != (root.stat().st_dev, root.stat().st_ino)
            or not isinstance(plan["groups"], list)
        ):
            raise ValueError("identity")
    except (ValueError, KeyError, TypeError) as exc:
        raise MigrationError("Invalid migration journal") from exc
    _require_resolved_journal_producers(plan)
    _require_supported_tag_updates(plan)
    _validate_journal_inode_flags(plan)
    _preflight_reviewed_files(root, plan)
    _preflight_resumed_inventory(root, plan)
    staging = _path(root, plan["staging"]["path"])
    if staging.exists() or staging.is_symlink():
        stage_dir = _staging_dir(root, plan)
        recorded_sidecars = {
            group["sidecar_prepared"]["name"]
            for group in plan["groups"]
            if isinstance(group.get("sidecar_prepared"), dict)
            and isinstance(group["sidecar_prepared"].get("name"), str)
        }
        recorded_audio = {
            tag["stage_name"]
            for group in plan["groups"]
            for tag in group.get("tags", [])
            if isinstance(tag.get("prepared"), dict) and isinstance(tag.get("stage_name"), str)
        }
        for item in stage_dir.iterdir():
            if item.name not in recorded_sidecars | recorded_audio:
                kind = "sidecar" if item.name.startswith("sidecar-") else "audio"
                raise MigrationError(f"Unjournalled {kind} stage requires manual review")
    for group in plan["groups"]:
        old_sidecar = _path(root, group["old_sidecar"])
        new_sidecar = _path(root, group["new_sidecar"])
        content = group["new_content"].encode("utf-8")
        if old_sidecar.parent != new_sidecar.parent or _digest(content) != group["new_hash"]:
            raise MigrationError("Invalid sidecar journal entry")
        if old_sidecar.exists():
            current = _regular(old_sidecar)
            current_hash = _digest(old_sidecar.read_bytes())
            is_original = (current.st_dev, current.st_ino) == (
                group.get("sidecar_dev"),
                group.get("sidecar_ino"),
            )
            if is_original:
                _require_original_sidecar(old_sidecar, group)
                if current_hash != group["old_hash"]:
                    raise MigrationError(f"Sidecar changed: {old_sidecar}")
            elif old_sidecar == new_sidecar and current_hash == group["new_hash"]:
                _require_prepared_sidecar(old_sidecar, group)
            else:
                raise MigrationError(f"Sidecar changed: {old_sidecar}")
        elif old_sidecar.is_symlink():
            raise MigrationError("Unsafe sidecar")
        elif not new_sidecar.exists():
            raise MigrationError("Both sidecar paths are missing")
        if new_sidecar != old_sidecar and (new_sidecar.exists() or new_sidecar.is_symlink()):
            _require_prepared_sidecar(new_sidecar, group)
        for entry in group["members"]:
            old = _path(root, entry["old"])
            new = _path(root, entry["new"])
            if old.parent != old_sidecar.parent or new.parent != old.parent or old == new:
                raise MigrationError("Invalid media journal entry")
            expected = (entry["dev"], entry["ino"], entry["size"], entry["mtime_ns"])
            old_exists = old.exists() or old.is_symlink()
            new_exists = new.exists() or new.is_symlink()
            if old_exists and not new_exists:
                st = _regular(old)
                _require_matching_flags(old, entry["flags"])
                if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != expected:
                    raise MigrationError(f"Source changed: {old}")
                _rename_no_replace(old, new)
                _fsync_dir(old.parent)
            elif not old_exists and new_exists:
                st = _regular(new)
                _require_matching_flags(new, entry["flags"])
                if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != expected:
                    matching_tags = [
                        tag
                        for tag in group.get("tags", [])
                        if tag["path"] == entry["new"] and tag.get("prepared")
                    ]
                    if len(matching_tags) != 1:
                        raise MigrationError(f"Destination changed: {new}")
                    prepared = matching_tags[0]["prepared"]
                    if (
                        (st.st_dev, st.st_ino, st.st_size)
                        != (
                            prepared["dev"],
                            prepared["ino"],
                            prepared["size"],
                        )
                        or (st.st_mtime_ns, int(st.st_mtime))
                        != _require_prepared_mtime(matching_tags[0], prepared)
                        or _file_hash(new) != prepared["hash"]
                    ):
                        raise MigrationError(f"Destination changed: {new}")
            else:
                raise MigrationError(f"Move conflict: {old} -> {new}")
            # Recovery must sync even if the rename happened in a previous run.
            _fsync_dir(old.parent)
        same_sidecar = old_sidecar == new_sidecar
        install = True
        for tag in group.get("tags", []):
            _retag_one(root, journal, plan, tag)
        if new_sidecar.exists() or new_sidecar.is_symlink():
            if same_sidecar and (new_sidecar.stat().st_dev, new_sidecar.stat().st_ino) == (
                group.get("sidecar_dev"),
                group.get("sidecar_ino"),
            ):
                _require_original_sidecar(new_sidecar, group)
                if _digest(new_sidecar.read_bytes()) == group["new_hash"]:
                    install = False
                elif _digest(new_sidecar.read_bytes()) != group["old_hash"]:
                    raise MigrationError(f"Sidecar changed: {new_sidecar}")
            else:
                _require_prepared_sidecar(new_sidecar, group)
                install = False
        if install:
            stage_dir = _staging_dir(root, plan)
            prepared = group.get("sidecar_prepared")
            if prepared:
                stage_name = prepared.get("name")
                if (
                    not isinstance(stage_name, str)
                    or Path(stage_name).name != stage_name
                    or not stage_name.startswith("sidecar-")
                    or not stage_name.endswith(".part")
                ):
                    raise MigrationError("Unsafe prepared sidecar name")
                temp = stage_dir / stage_name
                _require_prepared_sidecar(temp, group)
            else:
                fd, temp_name = tempfile.mkstemp(prefix="sidecar-", suffix=".part", dir=stage_dir)
                temp = Path(temp_name)
                with os.fdopen(fd, "wb") as stream:
                    os.fchown(stream.fileno(), group["sidecar_uid"], group["sidecar_gid"])
                    os.fchmod(stream.fileno(), group["sidecar_mode"])
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                _require_installed_sidecar(temp, group)
                _fsync_dir(stage_dir)
                staged = _regular(temp)
                group["sidecar_prepared"] = {
                    "name": temp.name,
                    "dev": staged.st_dev,
                    "ino": staged.st_ino,
                    "size": staged.st_size,
                }
                _save_journal(journal, plan)
            _require_prepared_sidecar(temp, group)
            _require_original_sidecar(old_sidecar, group)
            if _digest(old_sidecar.read_bytes()) != group["old_hash"]:
                raise MigrationError(f"Sidecar changed: {old_sidecar}")
            if same_sidecar:
                os.replace(temp, new_sidecar)
            else:
                _rename_no_replace(temp, new_sidecar)
            _require_prepared_sidecar(new_sidecar, group)
        # Re-sync both sides even when an earlier run installed the inode.
        _fsync_dir(new_sidecar.parent)
        _sync_recovered_staging(root, plan)
        if not same_sidecar and old_sidecar.exists():
            _require_original_sidecar(old_sidecar, group)
            if _digest(old_sidecar.read_bytes()) != group["old_hash"]:
                raise MigrationError(f"Sidecar changed: {old_sidecar}")
            old_sidecar.unlink()
        # Also sync when an earlier run already unlinked the old sidecar.
        _fsync_dir(old_sidecar.parent)
    if plan.get("staging"):
        stage_path = _path(root, plan["staging"]["path"])
        if stage_path.exists() or stage_path.is_symlink():
            _staging_dir(root, plan)
            stage_path.rmdir()
            _fsync_dir(stage_path.parent)
    _publish_completion(root, journal, plan)
    journal.unlink()
    _fsync_dir(journal.parent)
    return len(plan["groups"])
