"""Unintegrated Windows single-file publication research model.

NOT production code or a power-loss-durable protocol. It assumes a pre-existing,
externally bound journal file and honest file flushes, trusted private state,
all other writers stopped, fixed NTFS and a single fresh flat fixture. It
retains the journal/root handles and a real common byte lock during a session.
The Python capability is not proof against same-process lock manipulation.
No sidecars, migration, force replacement, repair, multiple operations,
physical-locality guarantee or monotonic witness are provided. Partial journal
records and unexplained files block without cleanup. A whole valid suffix can
be lost or rolled back without detection; directory-entry durability is not
established by these process tests.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as journal
import windows_recovery_model as recovery

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_HEADER_PREFIX = journal.MAGIC + b"ID="
_MAX_JOURNAL = 32768


class BindingError(RuntimeError):
    """An untrusted or ambiguous state requires manual inspection."""


@dataclass(frozen=True)
class ExternalBinding:
    volume_serial: int
    index_high: int
    index_low: int
    journal_id: str


def _valid_binding(binding: ExternalBinding) -> bool:
    return (
        isinstance(binding, ExternalBinding)
        and all(
            type(value) is int and 0 <= value <= 0xFFFFFFFF
            for value in (binding.volume_serial, binding.index_high, binding.index_low)
        )
        and (binding.index_high != 0 or binding.index_low != 0)
        and isinstance(binding.journal_id, str)
        and len(binding.journal_id) == 32
        and all(char in "0123456789abcdef" for char in binding.journal_id)
    )


class BoundPublisher:
    """Research-only one-file publisher with a held lock and journal handle."""

    def __init__(self, root: pathlib.Path, binding: ExternalBinding) -> None:
        if not _valid_binding(binding):
            raise BindingError("An externally supplied journal binding is required")
        self.root = pathlib.Path(root)
        self.binding = binding
        self.__lock: Any = None
        self.__journal: Any = None
        self.__pid = os.getpid()

    def __enter__(self):
        if self.__lock is not None:
            raise BindingError("Journal session cannot be entered more than once")
        if sys.platform != "win32":
            raise BindingError("Windows is required")
        import msvcrt

        self.__lock = recovery.LockedArchive(self.root)
        try:
            self.__lock.__enter__()
            api = self.__lock.api
            root_handle = api.open(str(self.root), directory=True, writable=False)
            try:
                root_volume = api.info(root_handle).volume_serial
            finally:
                api.close(root_handle)
            path = self.root / "journal.bin"
            handle = api.dll.CreateFileW(
                str(path),
                _GENERIC_READ | _GENERIC_WRITE | native._FILE_READ_ATTRIBUTES,
                native._SHARE_READ,
                None,
                native._OPEN_EXISTING,
                native._FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if handle is None or handle == native._INVALID_HANDLE:
                raise BindingError("Cannot open pre-existing journal")
            try:
                info = api.info(handle)
                if (
                    info.attributes
                    & (
                        native._ATTR_REPARSE
                        | native._ATTR_DIRECTORY
                        | native._ATTR_OFFLINE
                        | native._ATTR_RECALL_ON_OPEN
                        | native._ATTR_RECALL_ON_DATA_ACCESS
                    )
                    or info.links != 1
                    or info.volume_serial != root_volume
                    or (info.volume_serial, info.index_high, info.index_low)
                    != (self.binding.volume_serial, self.binding.index_high, self.binding.index_low)
                    or native._canonical_name(api.final_path(handle)) != str(path)
                ):
                    raise BindingError("Journal handle does not match external binding")
                fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
            except Exception:
                api.close(handle)
                raise
            try:
                self.__journal = os.fdopen(fd, "r+b", buffering=0)
            except Exception:
                os.close(fd)
                raise
            self._read_journal()
            return self
        except Exception:
            self._close()
            raise

    def _close(self) -> None:
        try:
            if self.__journal is not None:
                self.__journal.close()
        finally:
            if self.__lock is not None:
                self.__lock.__exit__(None, None, None)

    def __exit__(self, *_args) -> None:
        self._close()

    def _read_journal(self) -> bytes:
        if (
            self.__pid != os.getpid()
            or self.__lock is None
            or self.__journal is None
            or self.__journal.closed
        ):
            raise BindingError("Live lock and journal handle are required")
        self.__lock.verify()
        before = os.fstat(self.__journal.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_JOURNAL
        ):
            raise BindingError("Journal handle is unsafe or oversized")
        self.__journal.seek(0)
        raw = self.__journal.read(_MAX_JOURNAL + 1)
        after = os.fstat(self.__journal.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or len(raw) != before.st_size:
            raise BindingError("Journal changed while reading")
        header = _HEADER_PREFIX + self.binding.journal_id.encode("ascii") + b"\n"
        if not raw.startswith(header):
            raise BindingError("Journal header ID does not match external binding")
        return journal.MAGIC + raw[len(header) :]

    def _records(self) -> list[dict]:
        raw = self._read_journal()
        try:
            allowed = {recovery.LOCK, "journal.bin", "stage.bin", "target.bin"}
            if any(path.name not in allowed for path in self.root.iterdir()):
                raise ValueError("unexplained archive member")
            records = journal._parse(raw)
            if records:
                pending = records[0]
                identity, digest = pending["identity"], pending["sha256"]
                if (
                    not isinstance(identity, list)
                    or len(identity) != 3
                    or any(type(value) is not int or value < 0 for value in identity)
                    or identity[1] == 0
                    or identity[2] > 2 * 1024 * 1024
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                ):
                    raise ValueError("invalid intent identity or digest")
            return records
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BindingError("Journal state requires manual inspection") from exc

    def _sync_journal(self) -> None:
        self._read_journal()
        try:
            self.__journal.flush()
            os.fsync(self.__journal.fileno())
        except OSError as exc:
            raise BindingError("Journal flush failed; stop and inspect") from exc

    def _append(self, record: dict, phase: str, hook: Callable[[str], None]) -> None:
        self._read_journal()
        frame = journal._frame(record)
        self.__journal.seek(0, os.SEEK_END)
        if self.__journal.tell() + len(frame) > _MAX_JOURNAL:
            raise BindingError("Journal limit exceeded")
        split = len(frame) // 2
        try:
            if self.__journal.write(frame[:split]) != split:
                raise BindingError("Short journal write")
            hook(phase + "_partial")
            if self.__journal.write(frame[split:]) != len(frame) - split:
                raise BindingError("Short journal write")
            hook(phase + "_written")
            self._sync_journal()
            hook(phase + "_flushed")
        except OSError as exc:
            raise BindingError("Journal write failed; stop and inspect") from exc

    def _verified_file(self, path: pathlib.Path, pending: dict, *, flush: bool) -> None:
        try:
            payload, info = journal._verified_bytes(path, 2 * 1024 * 1024)
            identity = recovery._identity(info)
            if (
                list(identity) != pending["identity"]
                or hashlib.sha256(payload).hexdigest() != pending["sha256"]
            ):
                raise ValueError("identity or bytes differ")
            if flush:
                with path.open("r+b", buffering=0) as stream:
                    if recovery._identity(os.fstat(stream.fileno())) != identity:
                        raise ValueError("file changed before flush")
                    os.fsync(stream.fileno())
                    if recovery._identity(os.fstat(stream.fileno())) != identity:
                        raise ValueError("file changed during flush")
                checked, checked_info = journal._verified_bytes(path, 2 * 1024 * 1024)
                if recovery._identity(checked_info) != identity or checked != payload:
                    raise ValueError("file changed after flush")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BindingError("Publication file requires manual inspection") from exc

    def classify(self) -> str:
        """Observe visible state only; this never acknowledges a durable completion."""
        records = self._records()
        stage, target = self.root / "stage.bin", self.root / "target.bin"
        stage_exists, target_exists = os.path.lexists(stage), os.path.lexists(target)
        if not records:
            if stage_exists or target_exists:
                raise BindingError("Unjournalled output requires inspection")
            return "empty"
        if stage_exists == target_exists:
            raise BindingError("Missing or conflicting publication paths")
        selected = stage if stage_exists else target
        self._verified_file(selected, records[0], flush=False)
        if len(records) == 2:
            if selected != target:
                raise BindingError("Completion without published target")
            return "completion-frame-visible"
        return "pending-stage" if selected == stage else "pending-target"

    def begin(self, payload: bytes, hook: Callable[[str], None] = lambda _phase: None) -> str:
        if not isinstance(payload, bytes) or len(payload) > 2 * 1024 * 1024:
            raise BindingError("Invalid fixture payload")
        if self.classify() != "empty":
            raise BindingError("Fresh journal required")
        stage = self.root / "stage.bin"
        try:
            with stage.open("xb", buffering=0) as stream:
                hook("stage_created")
                if stream.write(payload) != len(payload):
                    raise BindingError("Short stage write")
                hook("stage_written")
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
                hook("stage_flushed")
        except OSError as exc:
            raise BindingError("Stage creation or flush failed; stop and inspect") from exc
        pending = {
            "phase": "pending",
            "stage": "stage.bin",
            "target": "target.bin",
            "identity": list(recovery._identity(info)),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        self._append(pending, "pending", hook)
        return self.recover(hook)

    def recover(self, hook: Callable[[str], None] = lambda _phase: None) -> str:
        records = self._records()
        state = self.classify()
        if state == "empty":
            raise BindingError("No pending operation")
        # A visible complete frame may follow a failed flush. Reflush before
        # publication or acknowledgement; this is not a power-loss proof.
        self._sync_journal()
        pending = records[0]
        stage, target = self.root / "stage.bin", self.root / "target.bin"
        if state == "pending-stage":
            self._verified_file(stage, pending, flush=True)
            self.__lock.verify()
            native.Adapter(self.__lock.api, archive_lock_held=True).publish(
                str(stage), str(target), archive_lock_held=True, staged_file_owned=True
            )
            hook("published")
        self._verified_file(target, pending, flush=True)
        if state != "completion-frame-visible":
            done = {
                "phase": "complete",
                "intent_sha256": hashlib.sha256(journal._record_bytes(pending)).hexdigest(),
            }
            self._append(done, "complete", hook)
        return "complete"
