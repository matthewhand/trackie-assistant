"""Unintegrated identity-ledger prototype; NOT a production or Windows protocol.

Only a pristine, empty, flat archive is supported. Existing-library import,
migration, force replacement, hardlinks, network filesystems and networking are
unsupported. This
prototype uses Linux flock and directory fsync; actual Windows locking and
power-loss guarantees require a separate reviewed protocol. Every cooperating
writer must hold this ledger's exclusive lock for its entire write operation.
This lock is NOT compatible with the existing archive writer lock or older
workers. Integration must coordinate explicitly with archive_locked_if_capped
and every older writer; it is unsupported until a complete protocol exists.
Hash chaining detects corruption, not adversarial rewrites; a lost suffix with
no surviving media cannot be detected without a separate durable witness.
Incomplete reserved outputs are refused for manual recovery, never adopted.
The test-only publisher exclusively creates its own files and retains their
open descriptors through completion; it cannot adopt preexisting files. This
does not establish safety against noncooperating-writer races, same-process
code that tampers with internals, or any Windows crash behavior. A Linux-only
staged no-replace model is used below, but recovery and production integration
are unsupported and require a complete independently reviewed protocol.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - this prototype refuses Windows
    fcntl = None  # type: ignore[assignment]

_LEDGER = ".trackie-identity-ledger"
_LOCK = ".trackie-identity-lock"
_ZERO = "0" * 64
_MAX_LEDGER = 32 * 1024 * 1024
_MAX_RECORD = 8192
_MEDIA = {".m4a", ".mp3", ".wav", ".mp4"}


class LedgerError(RuntimeError):
    """The prototype refused an unsafe or unsupported archive state."""


def _host() -> None:
    if not sys.platform.startswith("linux") or fcntl is None:
        raise LedgerError("Prototype supports Linux testing only; Windows durability is unverified")


def _root(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise LedgerError("Archive root must be a real directory")
    return path.resolve(strict=True)


def _file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LedgerError(f"Cannot inspect {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LedgerError(f"Unsupported file or hardlink: {path.name}")
    return info


def _safe_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", "..", _LEDGER, _LOCK}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
        or len(name.encode("utf-8")) > 240
    ):
        raise LedgerError("Unsafe or unsupported flat archive filename")
    return name


def _clip_id(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise LedgerError("Invalid canonical UUID") from exc
    return value


def _hash_file(path: Path) -> str:
    _file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = {}
    for key, value in pairs:
        if key in out:
            raise LedgerError("Duplicate JSON key")
        out[key] = value
    return out


def _record(previous: str, seq: int, payload: dict[str, Any]) -> bytes:
    body = {"previous": previous, "seq": seq, **payload}
    body["checksum"] = hashlib.sha256(_canonical(body)).hexdigest()
    line = _canonical(body) + b"\n"
    if len(line) > _MAX_RECORD:
        raise LedgerError("Ledger record too large")
    return line


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(fd, payload[offset:])
        if count <= 0:
            raise LedgerError("Short ledger write")
        offset += count
    os.fsync(fd)


def _lock(root: Path, *, create: bool) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(root / _LOCK, flags, 0o600)
    except OSError as exc:
        raise LedgerError("Cannot acquire identity lock") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LedgerError("Unsafe identity lock")
        if fcntl is None:
            raise LedgerError("File locking is unavailable")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LedgerError("Another identity writer holds the lock") from exc
        return fd
    except Exception:
        os.close(fd)
        raise


def _make_staging(root: Path) -> tuple[Path, tuple[int, int]]:
    stage = Path(tempfile.mkdtemp(prefix=".trackie-publish-", dir=root))
    info = stage.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_dev != root.stat().st_dev
    ):
        raise LedgerError("Unsafe or cross-filesystem staging directory")
    _sync_dir(root)
    return stage, (info.st_dev, info.st_ino)


def _verify_staging(root: Path, stage: Path, identity: tuple[int, int]) -> None:
    info = stage.lstat()
    if (
        stage.parent != root
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (info.st_dev, info.st_ino) != identity
        or info.st_dev != root.stat().st_dev
    ):
        raise LedgerError("Staging directory was replaced or is unsafe")


def _rename_no_replace(source: Path, destination: Path) -> None:
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise LedgerError("Atomic no-replace rename unavailable") from exc
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise LedgerError(f"Atomic no-replace publication failed: {os.strerror(error)}")


def _create_new(path: Path) -> int:
    """Create a new inode exclusively; never open an existing file for writing."""
    try:
        return os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError as exc:
        raise LedgerError(f"Cannot exclusively create {path.name}") from exc


def _write_created(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(fd, payload[offset:])
        if count <= 0:
            raise LedgerError("Short publication write")
        offset += count


def _sync_created(fd: int) -> None:
    os.fsync(fd)


def _verify_created(path: Path, fd: int, identity: tuple[int, int], expected: bytes) -> None:
    current = _file(path)
    opened = os.fstat(fd)
    if (
        (opened.st_dev, opened.st_ino) != identity
        or (current.st_dev, current.st_ino) != identity
        or opened.st_nlink != 1
    ):
        raise LedgerError(f"Exclusively created file was replaced: {path.name}")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while block := os.read(fd, 1024 * 1024):
        digest.update(block)
    if opened.st_size != len(expected) or digest.digest() != hashlib.sha256(expected).digest():
        raise LedgerError(f"Exclusively created file changed: {path.name}")


def _validate_sidecar_payload(payload: bytes, clip_id: str, audio: str) -> None:
    if len(payload) > 256 * 1024:
        raise LedgerError("Sidecar too large")
    try:
        data = json.loads(payload, object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeError) as exc:
        raise LedgerError("Invalid sidecar") from exc
    if (
        not isinstance(data, dict)
        or data.get("id") != clip_id
        or data.get("saved_files") != [audio]
    ):
        raise LedgerError("Sidecar identity or ownership does not match reservation")


def _validate_sidecar(path: Path, clip_id: str, audio: str) -> None:
    info = _file(path)
    if info.st_size > 256 * 1024:
        raise LedgerError("Sidecar too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, ValueError, UnicodeError) as exc:
        raise LedgerError("Invalid sidecar") from exc
    if (
        not isinstance(data, dict)
        or data.get("id") != clip_id
        or data.get("saved_files") != [audio]
    ):
        raise LedgerError("Sidecar identity or ownership does not match reservation")


class Ledger:
    """Exclusive, flat-archive test ledger. Never use for an existing library."""

    def __init__(self, root: Path, lock_fd: int) -> None:
        self.root = root
        self._lock_fd = lock_fd
        self._records: list[dict[str, Any]] = []
        self._reserved: dict[str, dict[str, Any]] = {}
        self._completed: dict[str, dict[str, Any]] = {}
        self._load()
        self.reconcile()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1

    def _assert_lock(self) -> None:
        if self._lock_fd < 0:
            raise LedgerError("Identity lock is not held")
        info = os.fstat(self._lock_fd)
        path_info = _file(self.root / _LOCK)
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise LedgerError("Identity lock was replaced")

    def _load(self) -> None:
        self._assert_lock()
        path = self.root / _LEDGER
        info = _file(path)
        if info.st_size > _MAX_LEDGER:
            raise LedgerError("Ledger too large")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LedgerError("Cannot read ledger") from exc
        after = _file(path)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise LedgerError("Ledger changed while reading")
        if not data or not data.endswith(b"\n"):
            raise LedgerError("Truncated ledger")
        previous = _ZERO
        records = []
        reserved: dict[str, dict[str, Any]] = {}
        completed: dict[str, dict[str, Any]] = {}
        names: set[str] = set()
        for seq, line in enumerate(data.splitlines(keepends=True)):
            if len(line) > _MAX_RECORD:
                raise LedgerError("Ledger record too large")
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicates)
            except (ValueError, UnicodeError) as exc:
                raise LedgerError("Corrupt ledger record") from exc
            if not isinstance(record, dict) or _canonical(record) + b"\n" != line:
                raise LedgerError("Noncanonical ledger record")
            checksum = record.get("checksum")
            body = {key: value for key, value in record.items() if key != "checksum"}
            if (
                record.get("seq") != seq
                or record.get("previous") != previous
                or checksum != hashlib.sha256(_canonical(body)).hexdigest()
            ):
                raise LedgerError("Ledger hash chain is invalid")
            kind = record.get("kind")
            common = {"seq", "previous", "checksum", "kind"}
            if seq == 0:
                if (
                    kind != "header"
                    or record.get("version") != 1
                    or set(record) != common | {"version"}
                ):
                    raise LedgerError("Invalid ledger header")
            elif kind == "reserve":
                if set(record) != common | {"id", "audio", "sidecar"}:
                    raise LedgerError("Invalid reservation")
                clip_id = _clip_id(record["id"])
                audio, sidecar = _safe_name(record["audio"]), _safe_name(record["sidecar"])
                if (
                    Path(audio).suffix.lower() not in _MEDIA
                    or sidecar != audio.rsplit(".", 1)[0] + ".json"
                ):
                    raise LedgerError("Unsupported reservation filenames")
                keys = {audio.casefold(), sidecar.casefold()}
                if clip_id in reserved or len(keys) != 2 or keys & names:
                    raise LedgerError("Duplicate identity or filename reservation")
                names.update(keys)
                reserved[clip_id] = record
            elif kind == "complete":
                if set(record) != common | {"id", "audio_sha256", "sidecar_sha256"}:
                    raise LedgerError("Invalid completion")
                clip_id = _clip_id(record["id"])
                if clip_id not in reserved or clip_id in completed:
                    raise LedgerError("Completion without unique reservation")
                for field in ("audio_sha256", "sidecar_sha256"):
                    value = record[field]
                    if (
                        not isinstance(value, str)
                        or len(value) != 64
                        or any(c not in "0123456789abcdef" for c in value)
                    ):
                        raise LedgerError("Invalid completion digest")
                completed[clip_id] = record
            else:
                raise LedgerError("Unknown ledger event")
            previous = checksum
            records.append(record)
        self._records, self._reserved, self._completed = records, reserved, completed

    def _append(self, payload: dict[str, Any]) -> None:
        self._assert_lock()
        path = self.root / _LEDGER
        expected = _file(path)
        line = _record(self._records[-1]["checksum"], len(self._records), payload)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        try:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino, actual.st_size) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
            ):
                raise LedgerError("Ledger changed before append")
            _write_all(fd, line)
        finally:
            os.close(fd)
        self._load()

    def reconcile(self, *, _allow_pending: str | None = None) -> None:
        """Refuse every orphan, foreign, altered or incomplete archive member."""
        self._assert_lock()
        allowed = {_LEDGER, _LOCK}
        for clip_id, record in self._reserved.items():
            audio, sidecar = record["audio"], record["sidecar"]
            audio_path, sidecar_path = self.root / audio, self.root / sidecar
            if clip_id in self._completed:
                completion = self._completed[clip_id]
                if _file(audio_path).st_size <= 1024:
                    raise LedgerError("Invalid audio file")
                _validate_sidecar(sidecar_path, clip_id, audio)
                if (
                    _hash_file(audio_path) != completion["audio_sha256"]
                    or _hash_file(sidecar_path) != completion["sidecar_sha256"]
                ):
                    raise LedgerError("Completed archive file changed")
                allowed.update((audio, sidecar))
            elif clip_id == _allow_pending:
                if _file(audio_path).st_size <= 1024:
                    raise LedgerError("Invalid pending audio")
                _validate_sidecar(sidecar_path, clip_id, audio)
                allowed.update((audio, sidecar))
            elif (
                audio_path.exists()
                or audio_path.is_symlink()
                or sidecar_path.exists()
                or sidecar_path.is_symlink()
            ):
                raise LedgerError("Incomplete reserved output requires manual recovery")
        for entry in self.root.iterdir():
            if entry.name not in allowed:
                raise LedgerError(f"Foreign or unsupported archive member: {entry.name}")
            _file(entry)

    def reserve(self, clip_id: str, audio: str, sidecar: str) -> None:
        self._load()
        self.reconcile()
        clip_id, audio, sidecar = _clip_id(clip_id), _safe_name(audio), _safe_name(sidecar)
        if Path(audio).suffix.lower() not in _MEDIA or sidecar != audio.rsplit(".", 1)[0] + ".json":
            raise LedgerError("Unsupported reservation filenames")
        keys = {audio.casefold(), sidecar.casefold()}
        used = {
            item[key].casefold() for item in self._reserved.values() for key in ("audio", "sidecar")
        }
        if clip_id in self._reserved or len(keys) != 2 or keys & used:
            raise LedgerError("Identity and filenames are permanently reserved")
        self._append({"kind": "reserve", "id": clip_id, "audio": audio, "sidecar": sidecar})

    def publish(self, clip_id: str, audio: bytes, sidecar: bytes) -> None:
        """Stage privately and publish with no-replace; test model only.

        Descriptor and inode identities remain private to this process. Any
        incomplete staging or publication requires manual recovery. No claim is
        made about Windows, existing archives, or uncooperative writer races.
        """
        self._load()
        self.reconcile()
        clip_id = _clip_id(clip_id)
        if clip_id not in self._reserved or clip_id in self._completed:
            raise LedgerError("Publication requires a unique pending reservation")
        if not isinstance(audio, bytes) or len(audio) <= 1024 or not isinstance(sidecar, bytes):
            raise LedgerError("Invalid publication payload")
        record = self._reserved[clip_id]
        _validate_sidecar_payload(sidecar, clip_id, record["audio"])
        audio_path, sidecar_path = self.root / record["audio"], self.root / record["sidecar"]
        stage, stage_identity = _make_staging(self.root)
        _verify_staging(self.root, stage, stage_identity)
        staged_audio, staged_sidecar = stage / "audio.part", stage / "sidecar.part"
        audio_fd = _create_new(staged_audio)
        try:
            audio_info = os.fstat(audio_fd)
            audio_identity = (audio_info.st_dev, audio_info.st_ino)
            sidecar_fd = _create_new(staged_sidecar)
            try:
                sidecar_info = os.fstat(sidecar_fd)
                sidecar_identity = (sidecar_info.st_dev, sidecar_info.st_ino)
                _write_created(audio_fd, audio)
                _write_created(sidecar_fd, sidecar)
                _sync_created(audio_fd)
                _sync_created(sidecar_fd)
                _sync_dir(stage)
                _verify_staging(self.root, stage, stage_identity)
                _verify_created(staged_audio, audio_fd, audio_identity, audio)
                _verify_created(staged_sidecar, sidecar_fd, sidecar_identity, sidecar)
                _rename_no_replace(staged_audio, audio_path)
                _sync_dir(self.root)
                _sync_dir(stage)
                _verify_created(audio_path, audio_fd, audio_identity, audio)
                _verify_staging(self.root, stage, stage_identity)
                _rename_no_replace(staged_sidecar, sidecar_path)
                _sync_dir(self.root)
                _sync_dir(stage)
                _verify_created(audio_path, audio_fd, audio_identity, audio)
                _verify_created(sidecar_path, sidecar_fd, sidecar_identity, sidecar)
                _verify_staging(self.root, stage, stage_identity)
                stage.rmdir()
                _sync_dir(self.root)
                self.reconcile(_allow_pending=clip_id)
                self._append(
                    {
                        "kind": "complete",
                        "id": clip_id,
                        "audio_sha256": hashlib.sha256(audio).hexdigest(),
                        "sidecar_sha256": hashlib.sha256(sidecar).hexdigest(),
                    }
                )
                self.reconcile()
            finally:
                os.close(sidecar_fd)
        finally:
            os.close(audio_fd)


def bootstrap(root: Path) -> Ledger:
    """Create a ledger only for a fully empty archive; never adopt existing files."""
    _host()
    root = _root(root)
    if any(root.iterdir()):
        raise LedgerError("Bootstrap requires a pristine, completely empty archive")
    lock_fd = _lock(root, create=True)
    try:
        if {p.name for p in root.iterdir()} != {_LOCK}:
            raise LedgerError("Archive changed during bootstrap")
        fd = os.open(
            root / _LEDGER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _write_all(fd, _record(_ZERO, 0, {"kind": "header", "version": 1}))
        finally:
            os.close(fd)
        _sync_dir(root)
        return Ledger(root, lock_fd)
    except Exception:
        os.close(lock_fd)
        raise


def open_ledger(root: Path) -> Ledger:
    """Lock, validate the entire ledger, and reconcile the whole archive."""
    _host()
    root = _root(root)
    lock_fd = _lock(root, create=False)
    try:
        return Ledger(root, lock_fd)
    except Exception:
        os.close(lock_fd)
        raise
