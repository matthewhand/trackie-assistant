"""WIP Win32 filesystem adapter model; not integrated and NOT Windows validated.

Uses CreateFileW, GetFileInformationByHandle, GetFinalPathNameByHandleW,
GetVolumePathNameW, GetDriveTypeW, GetVolumeInformationW, FlushFileBuffers,
and SetFileInformationByHandle(FileRenameInfo). This candidate rejects remote
paths, reparse points, alternate/short path spellings and file hardlinks.
It accepts only an explicitly asserted caller-held exclusive archive lock.

No claim is made about Windows power-loss durability, directory-entry flush,
concurrent uncooperative writers, storage firmware, file contents, identity
ledger integration, or correctness on a real Windows host. DRIVE_FIXED only
establishes a logical fixed drive, not that the underlying device is physically
local rather than network-backed. Errors after rename may have an ambiguous
outcome and require reconciliation, not a blind retry. Casefold-based alias
rejection is conservative but does not establish Windows Unicode equivalence
or behavior in case-sensitive directories. It is test-only.
"""

from __future__ import annotations

import ctypes
import ntpath
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Protocol

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
WCHARP = ctypes.c_wchar_p
_FILE_READ_ATTRIBUTES = 0x80
_DELETE = 0x10000
_GENERIC_WRITE = 0x40000000
_SHARE_READ = 1
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x02000000
_FLAG_OPEN_REPARSE_POINT = 0x00200000
_ATTR_DIRECTORY = 0x10
_ATTR_REPARSE = 0x400
_ATTR_OFFLINE = 0x1000
_ATTR_RECALL_ON_OPEN = 0x40000
_ATTR_RECALL_ON_DATA_ACCESS = 0x400000
_DRIVE_FIXED = 3
_FILE_RENAME_INFO = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class AdapterError(RuntimeError):
    """Unsupported, ambiguous, unsafe or failed filesystem operation."""


class _FileTime(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class _ByHandleInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("written", _FileTime),
        ("volume_serial", DWORD),
        ("size_high", DWORD),
        ("size_low", DWORD),
        ("links", DWORD),
        ("index_high", DWORD),
        ("index_low", DWORD),
    ]


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("replace_if_exists", BOOL),
        ("root_directory", HANDLE),
        ("filename_length", DWORD),
        ("filename", ctypes.c_uint16 * 1),
    ]


@dataclass(frozen=True)
class HandleInfo:
    attributes: int
    volume_serial: int
    links: int
    index_high: int
    index_low: int


class Win32Backend(Protocol):
    def volume(self, root: str) -> tuple[str, int, str]: ...
    def open(self, path: str, *, directory: bool, writable: bool) -> int: ...
    def info(self, handle: int) -> HandleInfo: ...
    def final_path(self, handle: int) -> str: ...
    def flush(self, handle: int) -> None: ...
    def rename_no_replace(self, handle: int, destination: str) -> None: ...
    def close(self, handle: int) -> None: ...


def _local_path(path: str) -> tuple[str, list[str]]:
    if not isinstance(path, str) or not path or "\x00" in path or "/" in path:
        raise AdapterError("Path is not a canonical local Win32 path")
    drive, tail = ntpath.splitdrive(path)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha():
        raise AdapterError("UNC, device, relative and non-drive paths are unsupported")
    if drive[0] != drive[0].upper() or not tail.startswith("\\"):
        raise AdapterError("Drive path must be absolute with an uppercase drive letter")
    try:
        if len(path.encode("utf-16-le")) // 2 > 240:
            raise AdapterError("Long or extended Win32 paths are unsupported")
    except UnicodeError as exc:
        raise AdapterError("Path contains invalid Unicode") from exc
    parts = tail[1:].split("\\") if tail != "\\" else []
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
        *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "¹²³"),
    }
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or part.split(".")[0].rstrip(" ").upper() in reserved
            or any(
                c in ':*?"<>|'
                or ord(c) < 32
                or 127 <= ord(c) <= 159
                or 0xD800 <= ord(c) <= 0xDFFF
                or 0x202A <= ord(c) <= 0x202E
                or 0x2066 <= ord(c) <= 0x2069
                or ord(c) in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
                for c in part
            )
        ):
            raise AdapterError("Unsafe, aliased or unsupported path component")
    return drive + "\\", parts


def _canonical_name(native_path: str) -> str:
    prefix = "\\\\?\\"
    if not native_path.startswith(prefix) or native_path.startswith(prefix + "UNC\\"):
        raise AdapterError("Handle does not resolve to a canonical local drive path")
    return native_path[len(prefix) :]


class Adapter:
    """Unintegrated candidate; caller must hold the archive lock throughout."""

    def __init__(self, api: Win32Backend, *, archive_lock_held: bool = False) -> None:
        if archive_lock_held is not True:
            raise AdapterError("Caller must hold the exclusive archive lock")
        self.api = api

    def publish(
        self,
        source: str,
        destination: str,
        *,
        archive_lock_held: bool = False,
        staged_file_owned: bool = False,
    ) -> None:
        """Flush and rename caller-asserted owned staging without replacement.

        The ownership assertion is not proof. A production publisher needs
        a reviewed exclusive-creation capability tied to this exact handle.
        """
        if archive_lock_held is not True:
            raise AdapterError("Caller must hold the exclusive archive lock")
        if staged_file_owned is not True:
            raise AdapterError("Caller must establish exclusive staging ownership")
        source_root, source_parts = _local_path(source)
        dest_root, dest_parts = _local_path(destination)
        if (
            not source_parts
            or not dest_parts
            or source_root != dest_root
            or source.casefold() == destination.casefold()
        ):
            raise AdapterError("Source and destination must be distinct files on the same drive")
        volume_root, drive_type, filesystem = self.api.volume(source_root)
        if volume_root != source_root or drive_type != _DRIVE_FIXED or filesystem != "NTFS":
            raise AdapterError("Only a canonical fixed NTFS drive is supported")
        with ExitStack() as stack:
            opened: dict[str, tuple[int, HandleInfo]] = {}

            def check(
                path: str, *, directory: bool, writable: bool = False
            ) -> tuple[int, HandleInfo]:
                handle = self.api.open(path, directory=directory, writable=writable)
                stack.callback(self.api.close, handle)
                info = self.api.info(handle)
                if info.attributes & (
                    _ATTR_REPARSE
                    | _ATTR_OFFLINE
                    | _ATTR_RECALL_ON_OPEN
                    | _ATTR_RECALL_ON_DATA_ACCESS
                ):
                    raise AdapterError("Reparse, offline or recall-on-access file is unsupported")
                if bool(info.attributes & _ATTR_DIRECTORY) != directory:
                    raise AdapterError("Unexpected file or directory type")
                if not directory and info.links != 1:
                    raise AdapterError("Hardlinked source file is unsupported")
                if _canonical_name(self.api.final_path(handle)) != path:
                    raise AdapterError("Path resolves through an alias or changed spelling")
                return handle, info

            root_handle, root_info = check(source_root, directory=True)
            opened[source_root] = (root_handle, root_info)
            for parts in (source_parts[:-1], dest_parts[:-1]):
                current = source_root
                for part in parts:
                    current = ntpath.join(current, part)
                    if current not in opened:
                        opened[current] = check(current, directory=True)
                    if opened[current][1].volume_serial != root_info.volume_serial:
                        raise AdapterError("Mounted or cross-volume directory is unsupported")
            source_handle, source_info = check(source, directory=False, writable=True)
            if source_info.volume_serial != root_info.volume_serial:
                raise AdapterError("Source is on another volume")
            self.api.flush(source_handle)
            # The API's FALSE ReplaceIfExists is the collision guard. No path
            # is deleted or replaced by the adapter.
            self.api.rename_no_replace(source_handle, destination)


class NativeWin32:
    """Experimental ctypes binding; never instantiate on non-Windows hosts.

    Supplying a mock DLL/error reader is only for Linux-hosted tests. A real
    DLL requires an explicit experimental opt-in and remains unvalidated.
    """

    def __init__(self, *, experimental: bool = False, dll=None, error_reader=None) -> None:
        if dll is None:
            if sys.platform != "win32" or not experimental:
                raise AdapterError("Native Win32 backend is unvalidated and disabled")
            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            error_reader = ctypes.get_last_error
        if error_reader is None:
            raise AdapterError("Mock backend requires an error reader")
        self.dll = dll
        self.error_reader = error_reader
        self._bind()

    def _bind(self) -> None:
        self.dll.CreateFileW.argtypes = [
            WCHARP,
            DWORD,
            DWORD,
            ctypes.c_void_p,
            DWORD,
            DWORD,
            HANDLE,
        ]
        self.dll.CreateFileW.restype = HANDLE
        self.dll.GetFileInformationByHandle.argtypes = [HANDLE, ctypes.POINTER(_ByHandleInfo)]
        self.dll.GetFileInformationByHandle.restype = BOOL
        self.dll.GetFinalPathNameByHandleW.argtypes = [
            HANDLE,
            ctypes.POINTER(ctypes.c_wchar),
            DWORD,
            DWORD,
        ]
        self.dll.GetFinalPathNameByHandleW.restype = DWORD
        self.dll.GetVolumePathNameW.argtypes = [WCHARP, ctypes.POINTER(ctypes.c_wchar), DWORD]
        self.dll.GetVolumePathNameW.restype = BOOL
        self.dll.GetDriveTypeW.argtypes = [WCHARP]
        self.dll.GetDriveTypeW.restype = DWORD
        self.dll.GetVolumeInformationW.argtypes = [
            WCHARP,
            ctypes.POINTER(ctypes.c_wchar),
            DWORD,
            ctypes.POINTER(DWORD),
            ctypes.POINTER(DWORD),
            ctypes.POINTER(DWORD),
            ctypes.POINTER(ctypes.c_wchar),
            DWORD,
        ]
        self.dll.GetVolumeInformationW.restype = BOOL
        self.dll.FlushFileBuffers.argtypes = [HANDLE]
        self.dll.FlushFileBuffers.restype = BOOL
        self.dll.SetFileInformationByHandle.argtypes = [
            HANDLE,
            ctypes.c_int32,
            ctypes.c_void_p,
            DWORD,
        ]
        self.dll.SetFileInformationByHandle.restype = BOOL
        self.dll.CloseHandle.argtypes = [HANDLE]
        self.dll.CloseHandle.restype = BOOL

    def _error(self, operation: str) -> AdapterError:
        return AdapterError(f"{operation} failed (Win32 error {self.error_reader()})")

    def volume(self, root: str) -> tuple[str, int, str]:
        volume_root = ctypes.create_unicode_buffer(32768)
        if not self.dll.GetVolumePathNameW(root, volume_root, len(volume_root)):
            raise self._error("GetVolumePathNameW")
        drive_type = self.dll.GetDriveTypeW(root)
        filesystem = ctypes.create_unicode_buffer(64)
        if not self.dll.GetVolumeInformationW(
            root, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            raise self._error("GetVolumeInformationW")
        return volume_root.value, drive_type, filesystem.value

    def open(self, path: str, *, directory: bool, writable: bool) -> int:
        access = _FILE_READ_ATTRIBUTES | ((_GENERIC_WRITE | _DELETE) if writable else 0)
        flags = _FLAG_OPEN_REPARSE_POINT | (_FLAG_BACKUP_SEMANTICS if directory else 0)
        handle = self.dll.CreateFileW(path, access, _SHARE_READ, None, _OPEN_EXISTING, flags, None)
        if handle is None or handle == _INVALID_HANDLE:
            raise self._error("CreateFileW")
        return int(handle)

    def info(self, handle: int) -> HandleInfo:
        info = _ByHandleInfo()
        if not self.dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise self._error("GetFileInformationByHandle")
        return HandleInfo(
            info.attributes, info.volume_serial, info.links, info.index_high, info.index_low
        )

    def final_path(self, handle: int) -> str:
        size = self.dll.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size or size > 32767:
            raise self._error("GetFinalPathNameByHandleW")
        output = ctypes.create_unicode_buffer(size + 1)
        written = self.dll.GetFinalPathNameByHandleW(handle, output, len(output), 0)
        if not written or written >= len(output):
            raise self._error("GetFinalPathNameByHandleW")
        return output.value

    def flush(self, handle: int) -> None:
        if not self.dll.FlushFileBuffers(handle):
            raise self._error("FlushFileBuffers")

    def rename_no_replace(self, handle: int, destination: str) -> None:
        try:
            encoded = destination.encode("utf-16-le")
        except UnicodeError as exc:
            raise AdapterError("Invalid destination Unicode") from exc
        offset = _RenameInfo.filename.offset
        # The Win32 path parser can inspect the UTF-16 tail as a terminated
        # string even though FileNameLength excludes the terminator. Without
        # the extra zero WCHAR native NTFS tests appended stray memory bytes.
        buffer = ctypes.create_string_buffer(offset + len(encoded) + ctypes.sizeof(ctypes.c_uint16))
        header = _RenameInfo.from_buffer(buffer)
        header.replace_if_exists = 0
        header.root_directory = None
        header.filename_length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        if not self.dll.SetFileInformationByHandle(handle, _FILE_RENAME_INFO, buffer, len(buffer)):
            raise self._error("SetFileInformationByHandle")

    def close(self, handle: int) -> None:
        if not self.dll.CloseHandle(handle):
            raise self._error("CloseHandle")
