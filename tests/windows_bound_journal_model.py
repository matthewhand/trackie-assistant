"""Unintegrated Windows bound-journal observation experiment.

NOT a production recovery protocol and NOT a power-loss durability proof.
The caller supplies a journal file ID and random header ID from a trusted,
independently retained source. This experiment opens the real common lock,
retains verified directory and journal handles, and only classifies one flat
single-file fixture. The journal never gets created, repaired or modified here.
Whole valid suffix rollback is not detectable without a separate monotonic
witness. All noncooperating writers and hostile same-process code are excluded;
Python encapsulation is not a kernel lock-ownership attestation. No sidecars,
multiple operations, media repair, migration or overwrites are supported.
"""

from __future__ import annotations

import os
import pathlib
import stat
import sys
from dataclasses import dataclass
from typing import Any

import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as journal
import windows_recovery_model as recovery

_GENERIC_READ = 0x80000000
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


class BoundJournalSession:
    """Own the common byte lock and retain the preprovisioned journal handle.

    This is an RAII convention for trusted cooperative code, not an attestation
    if same-process code explicitly unlocks or manipulates its private handles.
    """

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
                _GENERIC_READ | native._FILE_READ_ATTRIBUTES,
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
                fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            except Exception:
                api.close(handle)
                raise
            try:
                self.__journal = os.fdopen(fd, "rb", buffering=0)
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

    def classify(self) -> str:
        """Classify an existing fixture while holding the lock and journal."""
        raw = self._read_journal()
        try:
            allowed = {recovery.LOCK, "journal.bin", "stage.bin", "target.bin"}
            if any(path.name not in allowed for path in self.root.iterdir()):
                raise ValueError("unexplained archive member")
            records = journal._parse(raw)
            stage, target = self.root / "stage.bin", self.root / "target.bin"
            stage_exists, target_exists = os.path.lexists(stage), os.path.lexists(target)
            if not records:
                if stage_exists or target_exists:
                    raise ValueError("unexplained output")
                return "empty"
            if stage_exists == target_exists:
                raise ValueError("missing or conflicting outputs")
            selected = stage if stage_exists else target
            data, info = journal._verified_bytes(selected, 2 * 1024 * 1024)
            pending = records[0]
            identity = pending["identity"]
            digest = pending["sha256"]
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
            if (
                list(recovery._identity(info)) != pending["identity"]
                or __import__("hashlib").sha256(data).hexdigest() != pending["sha256"]
            ):
                raise ValueError("media identity or bytes differ")
            if len(records) == 2:
                if selected != target:
                    raise ValueError("completion without target")
                return "complete"
            return "pending-stage" if selected == stage else "pending-target"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BindingError("Journal state requires manual inspection") from exc
