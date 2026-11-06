"""Linux-only, unintegrated model of a durable operation intent.

This is not production code. The caller MUST hold Trackie's common archive
lock throughout every call AND the operation, and exclude older or
noncooperating writers. A boolean attests this precondition; it does not
verify a lock. The model never automatically recovers or resolves an intent.

A caller may publish only after begin() returns. It must keep the intent until
all related work and directory changes are complete and durable, and invoke
resolve_after_inspection() only after an explicit independent inspection. This
does not provide force-download, sidecar, metadata, retag or migration
integration. It assumes honest fsync, no arbitrary rollback or external
deletion of intent state, and no network/Windows power-loss guarantees.
After an I/O failure during resolution the caller must stop all writers and
require manual inspection, even if the marker is no longer visible.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from pathlib import Path

_NAME = "tag-operation-intent.json"
_MAX_BYTES = 65536


class UnsafeOperationIntent(RuntimeError):
    """An operation or intent cannot be proven safe."""


def _directory(root: Path, lock_held: bool) -> Path:
    if lock_held is not True:
        raise UnsafeOperationIntent("caller must hold common archive lock")
    if sys.platform != "linux":
        raise UnsafeOperationIntent("model requires Linux")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise UnsafeOperationIntent("required file operations unavailable")
    root = Path(root)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise UnsafeOperationIntent("archive root must be absolute and contain no aliases")
    except OSError as exc:
        raise UnsafeOperationIntent("cannot resolve archive root") from exc
    for path in (root, root / ".library"):
        try:
            info = path.lstat()
        except OSError as exc:
            raise UnsafeOperationIntent("archive and metadata directories must exist") from exc
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise UnsafeOperationIntent("unsafe archive or metadata directory")
    return root / ".library"


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(marker: Path) -> tuple[bytes, os.stat_result] | None:
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeOperationIntent("unsafe or unreadable intent") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_BYTES:
            raise UnsafeOperationIntent("unsafe or oversized intent")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) != info.st_size:
            raise UnsafeOperationIntent("intent changed while reading")
        return raw, info
    except OSError as exc:
        raise UnsafeOperationIntent("cannot read intent") from exc
    finally:
        os.close(fd)


def _parse(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "id", "operation", "paths"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or str(uuid.UUID(data["id"])) != data["id"]
            or not isinstance(data["operation"], str)
            or not data["operation"]
            or not isinstance(data["paths"], list)
            or not data["paths"]
        ):
            raise ValueError
        for value in data["paths"]:
            if not isinstance(value, str) or not value or "\\" in value:
                raise ValueError
            path = Path(value)
            if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
                raise ValueError
        if len(data["paths"]) != len(set(data["paths"])):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise UnsafeOperationIntent("invalid or incomplete intent; manual review required") from exc


def inspect_pending(root: Path, *, lock_held: bool) -> dict | None:
    """Read pending state without modifying it; invalid state blocks writers."""
    directory = _directory(root, lock_held)
    found = _read(directory / _NAME)
    if found is None:
        return None
    return _parse(found[0])


def begin(root: Path, operation: str, paths: list[str], *, lock_held: bool) -> str:
    """Fsync a new intent and its directory before the caller may publish."""
    directory = _directory(root, lock_held)
    marker = directory / _NAME
    data = {
        "version": 1,
        "id": str(uuid.uuid4()),
        "operation": operation,
        "paths": paths,
    }
    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise UnsafeOperationIntent("intent too large")
    _parse(raw)
    fd = None
    try:
        _sync_dir(Path(root))
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_dir(directory)
    except OSError as exc:
        # Retain any created file, including an incomplete one. No publication
        # is authorized unless the complete intent and directory fsync succeed.
        raise UnsafeOperationIntent("cannot durably create intent; manual review required") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return str(data["id"])


def resolve_after_inspection(root: Path, intent_id: str, *, lock_held: bool) -> None:
    """Remove only the exact inspected intent; never called automatically.

    The operator must first verify and durably sync all affected state. An
    unlink or directory-fsync error has an ambiguous outcome and requires all
    writers to stay stopped regardless of whether the path remains visible.
    """
    directory = _directory(root, lock_held)
    marker = directory / _NAME
    found = _read(marker)
    if found is None:
        raise UnsafeOperationIntent("intent is missing; manual review required")
    raw, info = found
    if _parse(raw)["id"] != intent_id:
        raise UnsafeOperationIntent("intent identity does not match inspection")
    try:
        current = marker.lstat()
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise UnsafeOperationIntent("intent changed after inspection")
        marker.unlink()
        _sync_dir(directory)
    except OSError as exc:
        raise UnsafeOperationIntent(
            "intent resolution durability uncertain; stop all writers and inspect"
        ) from exc
