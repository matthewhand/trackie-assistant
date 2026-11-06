"""Cross-process lock for archive writers (standard library, Windows/Linux/macOS).

Keep the lock file: unlinking a locked inode would let a second process lock a
*different* inode under the same name. Media scanners ignore this dotfile.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import functools
import inspect
import os
import stat
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

_WAIT_SEC = 30.0
_POLL_SEC = 0.10
_HELD_ARCHIVE: contextvars.ContextVar[tuple[Path, asyncio.Task[Any] | None] | None] = (
    contextvars.ContextVar("suno_held_archive", default=None)
)


def _try_lock(file: Any) -> bool:
    file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN) or getattr(exc, "winerror", None) in (
            32,
            33,
            36,
        ):
            return False
        raise


def _unlock(file: Any) -> None:
    file.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _open_archive_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    name = root / ".suno-sync.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if name.is_symlink():
        raise OSError(f"Unsafe archive lock symlink: {name}")
    fd = os.open(name, flags, 0o600)
    try:
        opened = os.fstat(fd)
        named = name.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError(f"Unsafe linked, replaced or nonregular archive lock: {name}")
        if opened.st_size == 0:
            os.write(fd, b"0")
            os.fsync(fd)
        return os.fdopen(fd, "r+b", buffering=0)
    except BaseException:
        os.close(fd)
        raise


@asynccontextmanager
async def _exclusive_archive(root: Path):
    with _open_archive_lock(root) as file:
        deadline = time.monotonic() + _WAIT_SEC
        locked = False
        while not locked:
            locked = _try_lock(file)
            if locked:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Archive sync lock busy for {_WAIT_SEC:g}s: {root}")
            await asyncio.sleep(_POLL_SEC)
        try:
            yield
        finally:
            _unlock(file)


@contextmanager
def _exclusive_archive_sync(root: Path):
    """Take the same lock as async writers for synchronous migration work."""
    with _open_archive_lock(root) as file:
        deadline = time.monotonic() + _WAIT_SEC
        while not _try_lock(file):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Archive sync lock busy for {_WAIT_SEC:g}s: {root}")
            time.sleep(_POLL_SEC)
        try:
            yield
        finally:
            _unlock(file)


def raise_if_unsupported_archive_write() -> None:
    """Reject unsupported Windows writes before any archive bookkeeping."""
    if sys.platform == "win32":
        from .download_intent import WindowsArchiveWriteUnavailable

        raise WindowsArchiveWriteUnavailable(
            "Windows Trackie archive writes are unavailable until crash-safe recovery is supported"
        )


def archive_locked_if_capped(func: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize real archive writes that share an output root.

    Keep the historical decorator name for compatibility. Dry runs do not
    create or wait on a lock. All cooperating real writers use this lock.
    """
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        values = signature.bind(*args, **kwargs)
        values.apply_defaults()
        if values.arguments["dry_run"]:
            return await func(*args, **kwargs)
        root = Path(values.arguments["output_dir"]).expanduser().resolve()
        from .producer_migration import raise_if_migration_pending

        # Temporary fail-closed gate, not Windows support.
        raise_if_unsupported_archive_write()
        owner = (root, asyncio.current_task())
        if _HELD_ARCHIVE.get() == owner:
            return await func(*args, **kwargs)
        async with _exclusive_archive(root):
            raise_if_migration_pending(root)
            token = _HELD_ARCHIVE.set(owner)
            try:
                return await func(*args, **kwargs)
            finally:
                _HELD_ARCHIVE.reset(token)

    return guarded
