"""Durable clip-id reservations for interrupted producer-named downloads.

An intent records only identity and relative destination, never media or credentials.
A leftover intent with media is deliberately fail-closed for manual recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .urls import UUID_RE

_MEDIA_SUFFIXES = (".m4a", ".mp3", ".wav", ".mp4")


class UnsafeDownloadIntent(RuntimeError):
    """An interrupted or unsafe reservation requires manual investigation."""


class WindowsArchiveWriteUnavailable(UnsafeDownloadIntent):
    """Windows archive writes are blocked pending crash-safe recovery."""


def sync_directory(path: Path) -> None:
    """Require a durable directory entry before media can be published."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise UnsafeDownloadIntent("filesystem cannot persist intent directory") from exc


def _directory(root: Path) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise UnsafeDownloadIntent("archive root does not exist")
    library = root / ".library"
    intents = library / "download-intents"
    for part in (library, intents):
        if part.is_symlink():
            raise UnsafeDownloadIntent("intent directory is a symlink")
        existed = part.exists()
        part.mkdir(mode=0o700, exist_ok=True)
        if not existed:
            sync_directory(part.parent)
        if not part.is_dir() or not part.resolve().is_relative_to(root):
            raise UnsafeDownloadIntent("invalid intent directory")
    return intents


def _marker(root: Path, clip_id: str) -> Path:
    clip_id = clip_id.lower()
    if not UUID_RE.fullmatch(clip_id):
        raise UnsafeDownloadIntent("invalid clip ID")
    return _directory(root) / (clip_id + ".json")


def _base(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise UnsafeDownloadIntent("invalid intent path")
    part = Path(relative)
    if part.is_absolute() or any(p in (".", "..") for p in part.parts):
        raise UnsafeDownloadIntent("invalid intent path")
    root = root.resolve()
    base = root / part
    if base.is_symlink() or not base.parent.resolve().is_relative_to(root):
        raise UnsafeDownloadIntent("intent escapes archive")
    current = root
    for component in part.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise UnsafeDownloadIntent("intent crosses symlink")
    return base


def prepare(root: Path, clip_id: str) -> bool:
    """Reject an interrupted publication; discard only an empty reservation."""
    marker = _marker(root, clip_id)
    if not marker.exists() and not marker.is_symlink():
        return False
    try:
        if marker.is_symlink() or marker.stat().st_size > 16_384:
            raise UnsafeDownloadIntent("unsafe intent file")
        data = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("id") != clip_id.lower():
            raise UnsafeDownloadIntent("invalid intent identity")
        base = _base(root, data.get("base"))
        occupied = []
        for suffix in (*_MEDIA_SUFFIXES, ".json"):
            path = Path(str(base) + suffix)
            if path.exists() or path.is_symlink():
                occupied.append(path)
        if occupied:
            if data.get("complete") is True:
                expected = data.get("media")
                if not isinstance(expected, dict) or not expected:
                    raise UnsafeDownloadIntent("completed intent lacks file fingerprints")
                actual = {p.name: p for p in occupied if p.suffix.lower() in _MEDIA_SUFFIXES}
                if set(actual) != set(expected):
                    raise UnsafeDownloadIntent("completed intent media has changed")
                for name, path in actual.items():
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or _fingerprint(path) != expected[name]
                    ):
                        raise UnsafeDownloadIntent("completed intent media has changed")
                return True
            raise UnsafeDownloadIntent("interrupted publication requires review")
        if data.get("complete") is True:
            raise UnsafeDownloadIntent("completed media is missing")
        marker.unlink()
        sync_directory(marker.parent)
        return False
    except (OSError, ValueError, TypeError) as exc:
        raise UnsafeDownloadIntent("cannot safely inspect intent") from exc


def reserve(root: Path, clip_id: str, base: Path) -> Path:
    """Reserve a destination with exclusive creation before starting media I/O."""
    root = root.resolve()
    try:
        relative = base.relative_to(root).as_posix()
    except ValueError as exc:
        raise UnsafeDownloadIntent("destination outside archive") from exc
    _base(root, relative)
    marker = _marker(root, clip_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    data = json.dumps({"schema": 1, "id": clip_id.lower(), "base": relative}) + "\n"
    try:
        fd = os.open(marker, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(marker.parent)
    except OSError as exc:
        raise UnsafeDownloadIntent("cannot reserve clip ID") from exc
    return marker


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def completed_owner(root: Path, base: Path) -> str | None:
    """Find a uniquely verified completed intent for a filename."""
    intents = _directory(root)
    matches = []
    for marker in intents.glob("*.json"):
        if marker.is_symlink() or not UUID_RE.fullmatch(marker.stem):
            raise UnsafeDownloadIntent("invalid completed intent")
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise UnsafeDownloadIntent("invalid completed intent")
            if (
                data.get("complete") is True
                and str(_base(root, data.get("base"))).casefold() == str(base).casefold()
                and prepare(root, marker.stem)
            ):
                matches.append(marker.stem)
        except (OSError, ValueError, TypeError) as exc:
            raise UnsafeDownloadIntent("cannot inspect completed intent") from exc
    if len(matches) > 1:
        raise UnsafeDownloadIntent("conflicting completed intents")
    return matches[0] if matches else None


def has_reserved_base(root: Path, base: Path) -> bool:
    """Identify any intent owning a stem without mutating or trusting its media."""
    root = root.resolve()
    library = root / ".library"
    intents = library / "download-intents"
    for directory in (library, intents):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise UnsafeDownloadIntent("unsafe intent directory")
        if not directory.exists():
            return False
    for marker in intents.glob("*.json"):
        try:
            if (
                marker.is_symlink()
                or not UUID_RE.fullmatch(marker.stem)
                or marker.stat().st_size > 16_384
            ):
                raise UnsafeDownloadIntent("invalid intent while checking extraction")
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("id") != marker.stem:
                raise UnsafeDownloadIntent("invalid intent while checking extraction")
            if str(_base(root, data.get("base"))).casefold() == str(base).casefold():
                return True
        except (OSError, ValueError, TypeError) as exc:
            raise UnsafeDownloadIntent("cannot inspect extraction intent") from exc
    return False


def complete_without_sidecar(marker: Path, base: Path, owned_names: set[str]) -> None:
    """Keep a durable UUID record when the caller disabled visible sidecars."""
    tmp: Path | None = None
    try:
        if marker.is_symlink() or marker.stat().st_size > 16_384:
            raise UnsafeDownloadIntent("unsafe intent file")
        data = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise UnsafeDownloadIntent("invalid intent file")
        media = [Path(str(base) + suffix) for suffix in _MEDIA_SUFFIXES]
        media = [path for path in media if path.exists() or path.is_symlink()]
        if (
            not media
            or {path.name for path in media} != owned_names
            or any(path.is_symlink() or not path.is_file() for path in media)
        ):
            raise UnsafeDownloadIntent("cannot fingerprint unowned media")
        data["media"] = {path.name: _fingerprint(path) for path in media}
        data["complete"] = True
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marker.parent,
            prefix=".suno-intent-",
            suffix=".part",
            delete=False,
        ) as stream:
            tmp = Path(stream.name)
            stream.write(json.dumps(data) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(marker)
        tmp = None
        sync_directory(marker.parent)
    except (OSError, ValueError, TypeError) as exc:
        raise UnsafeDownloadIntent("cannot finish clip reservation") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
