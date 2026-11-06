"""Unintegrated Windows single-file process-recovery experiment.

NOT a production protocol or a power-loss-durable journal. Directory flushes
are experimental: success has not established NTFS power-loss ordering. Only
a fresh, flat, test archive with an existing common lock file is supported.
Every other writer must be stopped; physical storage locality is an external
precondition. No sidecars, downloads, migration, force overwrite, cleanup,
concurrent uncooperative writers or arbitrary rollback are supported. Retain
all records permanently. A partial record, uncertain identity or unexplained
state blocks further progress. The live lock object owns a real OS byte lock;
it is not a security boundary against hostile code in the same process.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import sys
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import windows_filesystem_adapter_model as win

LOCK = ".suno-sync.lock"
PENDING = ".trackie-research-pending.json"
READY = ".trackie-research-ready.json"
COMPLETE = ".trackie-research-complete.json"
MAX_FILE = 2 * 1024 * 1024


class RecoveryError(RuntimeError):
    """An unsupported or ambiguous state requires manual review."""


def _host() -> None:
    if sys.platform != "win32":
        raise RecoveryError("Windows is required for this research model")


def _regular(path: pathlib.Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RecoveryError(f"Cannot inspect {path.name}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & win._ATTR_REPARSE
        or not info.st_ino
    ):
        raise RecoveryError(f"Unsafe or unidentifiable file: {path.name}")
    return info


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_size


def _read_file(path: pathlib.Path, limit: int) -> tuple[bytes, os.stat_result]:
    before = _regular(path)
    if before.st_size > limit:
        raise RecoveryError("File exceeds research limit")
    try:
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise RecoveryError("File changed while opening")
            raw = stream.read(limit + 1)
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise RecoveryError("File changed while reading")
        if _identity(_regular(path)) != _identity(before) or len(raw) != before.st_size:
            raise RecoveryError("File changed after reading")
    except OSError as exc:
        raise RecoveryError("Cannot read file safely") from exc
    return raw, before


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("Duplicate journal key")
        result[key] = value
    return result


def _read_record(path: pathlib.Path, keys: set[str]) -> dict:
    raw, _ = _read_file(path, 8192)
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicates)
        if (
            not isinstance(data, dict)
            or set(data) != keys
            or type(data.get("version")) is not int
            or data.get("version") != 1
        ):
            raise ValueError("bad record fields")
        return data
    except (ValueError, UnicodeError, TypeError) as exc:
        raise RecoveryError("Invalid journal record") from exc


def _flush_existing(path: pathlib.Path) -> None:
    """Reflush a verified file after an earlier interrupted or failed flush."""
    before = _regular(path)
    try:
        with path.open("r+b", buffering=0) as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise RecoveryError("File changed before reflush")
            os.fsync(stream.fileno())
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise RecoveryError("File changed during reflush")
        if _identity(_regular(path)) != _identity(before):
            raise RecoveryError("File changed after reflush")
    except OSError as exc:
        raise RecoveryError("File reflush failed; manual review required") from exc


def _flush_directory(api: win.NativeWin32, root: pathlib.Path) -> None:
    """Experimental call; a successful return is not a power-loss guarantee."""
    handle = api.open(str(root), directory=True, writable=True)
    try:
        api.flush(handle)
    finally:
        api.close(handle)


def _record_bytes(record: dict) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _record_digest(record: dict) -> str:
    return hashlib.sha256(_record_bytes(record)).hexdigest()


def _write_record(api: win.NativeWin32, root: pathlib.Path, name: str, record: dict) -> None:
    payload = _record_bytes(record)
    try:
        with (root / name).open("xb", buffering=0) as stream:
            if stream.write(payload) != len(payload):
                raise RecoveryError("Short journal write")
            os.fsync(stream.fileno())
        _flush_directory(api, root)
    except OSError as exc:
        raise RecoveryError("Journal write or flush failed; manual review required") from exc


class _ResearchWin32(win.NativeWin32):
    """Keep directory handles open while permitting an experimental flush.

    Directory handles allow read/write sharing but deny delete sharing, so
    cooperating path traversals can coexist and held directories cannot be
    renamed by ordinary Win32 opens. This does not prove power-loss safety.
    """

    def open(self, path: str, *, directory: bool, writable: bool) -> int:
        if not directory:
            return super().open(path, directory=False, writable=writable)
        access = win._FILE_READ_ATTRIBUTES | (win._GENERIC_WRITE if writable else 0)
        flags = win._FLAG_OPEN_REPARSE_POINT | win._FLAG_BACKUP_SEMANTICS
        handle = self.dll.CreateFileW(
            path, access, win._SHARE_READ | 2, None, win._OPEN_EXISTING, flags, None
        )
        if handle is None or handle == win._INVALID_HANDLE:
            raise self._error("CreateFileW directory")
        return int(handle)


class LockedArchive:
    """Own a real byte-range lock and keep verified path handles open."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.api = _ResearchWin32(experimental=True)
        self.stack = ExitStack()
        self.file: Any = None
        self.pid = os.getpid()

    def __enter__(self):
        _host()
        import msvcrt

        drive, parts = win._local_path(str(self.root))
        if not parts:
            raise RecoveryError("Drive root is not a supported archive")
        volume, drive_type, filesystem = self.api.volume(drive)
        if volume != drive or drive_type != win._DRIVE_FIXED or filesystem != "NTFS":
            raise RecoveryError("Only fixed NTFS is supported")
        current = drive
        try:
            expected_volume = None
            for part in [None, *parts]:
                if part is not None:
                    current = str(pathlib.PureWindowsPath(current) / part)
                handle = self.api.open(current, directory=True, writable=False)
                self.stack.callback(self.api.close, handle)
                info = self.api.info(handle)
                if (
                    info.attributes & (win._ATTR_REPARSE | win._ATTR_OFFLINE)
                    or not info.attributes & win._ATTR_DIRECTORY
                    or win._canonical_name(self.api.final_path(handle)) != current
                ):
                    raise RecoveryError("Unsafe or aliased archive directory")
                if expected_volume is None:
                    expected_volume = info.volume_serial
                elif info.volume_serial != expected_volume:
                    raise RecoveryError("Cross-volume archive is unsupported")
            lock_path = self.root / LOCK
            before = _regular(lock_path)
            self.file = self.stack.enter_context(lock_path.open("r+b", buffering=0))
            if _identity(os.fstat(self.file.fileno())) != _identity(before) or before.st_size < 1:
                raise RecoveryError("Archive lock identity changed")
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RecoveryError("Archive lock is already held") from exc
            self.stack.callback(self._unlock)
            self.lock_identity = _identity(before)
            return self
        except Exception:
            self.stack.close()
            raise

    def _unlock(self) -> None:
        import msvcrt

        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]

    def verify(self) -> None:
        if self.file is None or self.file.closed or self.pid != os.getpid():
            raise RecoveryError("Live archive lock required")
        try:
            if (
                _identity(os.fstat(self.file.fileno())) != self.lock_identity
                or _identity(_regular(self.root / LOCK)) != self.lock_identity
            ):
                raise RecoveryError("Archive lock identity changed")
        except OSError as exc:
            raise RecoveryError("Archive lock is unavailable") from exc

    def __exit__(self, *_args) -> None:
        self.stack.close()


def _validate_pending(data: dict, root: pathlib.Path) -> None:
    try:
        if str(uuid.UUID(data["id"])) != data["id"]:
            raise ValueError
        if data["stage"] != ".trackie-stage-" + data["id"]:
            raise ValueError
        target = data["target"]
        if not isinstance(target, str) or not target or target.startswith("."):
            raise ValueError
        root_path, parts = win._local_path(str(root / target))
        del root_path
        if len(parts) != len(win._local_path(str(root))[1]) + 1 or parts[-1] != target:
            raise ValueError
        if type(data["size"]) is not int or not 0 <= data["size"] <= MAX_FILE:
            raise ValueError
        if not isinstance(data["sha256"], str) or len(data["sha256"]) != 64:
            raise ValueError
        bytes.fromhex(data["sha256"])
    except (KeyError, TypeError, ValueError, AttributeError, win.AdapterError) as exc:
        raise RecoveryError("Invalid pending record") from exc


def _verify_payload(path: pathlib.Path, pending: dict, ready: dict) -> None:
    raw, info = _read_file(path, MAX_FILE)
    if (
        list(_identity(info)) != ready["identity"]
        or len(raw) != pending["size"]
        or hashlib.sha256(raw).hexdigest() != pending["sha256"]
    ):
        raise RecoveryError("Staged or installed file identity/content differs")


def recover(locked: LockedArchive, hook: Callable[[str], None] = lambda _phase: None) -> str:
    locked.verify()
    root, api = locked.root, locked.api
    pending = _read_record(root / PENDING, {"version", "id", "stage", "target", "size", "sha256"})
    _validate_pending(pending, root)
    pending_digest = _record_digest(pending)
    ready = _read_record(root / READY, {"version", "id", "identity", "pending_sha256"})
    if (
        ready["id"] != pending["id"]
        or ready["pending_sha256"] != pending_digest
        or not isinstance(ready["identity"], list)
        or len(ready["identity"]) != 3
        or any(type(value) is not int or value < 0 for value in ready["identity"])
    ):
        raise RecoveryError("Invalid ready record")
    # A prior writer may have failed after writing bytes but before fsync.
    # Reflush both valid records and their directory before any publication.
    _flush_existing(root / PENDING)
    _flush_existing(root / READY)
    _flush_directory(api, root)
    stage, target = root / pending["stage"], root / pending["target"]
    completed = root / COMPLETE
    if completed.exists():
        done = _read_record(
            completed, {"version", "id", "sha256", "pending_sha256", "ready_sha256"}
        )
        if (
            done["id"] != pending["id"]
            or done["sha256"] != pending["sha256"]
            or done["pending_sha256"] != pending_digest
            or done["ready_sha256"] != _record_digest(ready)
            or stage.exists()
        ):
            raise RecoveryError("Completion record conflicts with files")
        _verify_payload(target, pending, ready)
        _flush_existing(target)
        _flush_existing(completed)
        _flush_directory(api, root)
        return "complete"
    if stage.exists() and not target.exists():
        _verify_payload(stage, pending, ready)
        _flush_existing(stage)
        _flush_directory(api, root)
        locked.verify()
        win.Adapter(api, archive_lock_held=True).publish(
            str(stage), str(target), archive_lock_held=True, staged_file_owned=True
        )
        hook("published")
    elif stage.exists() or not target.exists():
        raise RecoveryError("Conflicting or missing publication paths")
    _verify_payload(target, pending, ready)
    _flush_existing(target)
    _flush_directory(api, root)
    _write_record(
        api,
        root,
        COMPLETE,
        {
            "version": 1,
            "id": pending["id"],
            "sha256": pending["sha256"],
            "pending_sha256": pending_digest,
            "ready_sha256": _record_digest(ready),
        },
    )
    hook("complete")
    return "complete"


def start(
    locked: LockedArchive,
    target: str,
    payload: bytes,
    hook: Callable[[str], None] = lambda _phase: None,
) -> str:
    locked.verify()
    root, api = locked.root, locked.api
    if not isinstance(payload, bytes) or len(payload) > MAX_FILE:
        raise RecoveryError("Invalid test payload")
    if {p.name for p in root.iterdir()} != {LOCK}:
        raise RecoveryError("Research archive must contain only its lock")
    identifier = str(uuid.uuid4())
    pending: dict[str, Any] = {
        "version": 1,
        "id": identifier,
        "stage": ".trackie-stage-" + identifier,
        "target": target,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    _validate_pending(pending, root)
    _write_record(api, root, PENDING, pending)
    hook("pending")
    stage = root / pending["stage"]
    with stage.open("xb", buffering=0) as stream:
        if stream.write(payload) != len(payload):
            raise RecoveryError("Short stage write")
        os.fsync(stream.fileno())
        identity = _identity(os.fstat(stream.fileno()))
    _flush_directory(api, root)
    hook("stage")
    ready = {
        "version": 1,
        "id": identifier,
        "identity": list(identity),
        "pending_sha256": _record_digest(pending),
    }
    _write_record(api, root, READY, ready)
    hook("ready")
    return recover(locked, hook)
