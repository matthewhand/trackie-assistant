"""Lock initialization must never alter an unrelated inode."""

import os
from pathlib import Path

import pytest

from suno_dl.archive_lock import _exclusive_archive, _exclusive_archive_sync


def hardlinked(root):
    root.mkdir()
    target = root.parent / "unrelated-empty"
    target.write_bytes(b"")
    os.link(target, root / ".suno-sync.lock")
    return target


def test_sync_lock_refuses_hardlink_before_initialization(tmp_path):
    root = tmp_path / "archive"
    target = hardlinked(root)
    with pytest.raises(OSError, match="Unsafe"), _exclusive_archive_sync(root):
        pass
    assert target.read_bytes() == b"" and target.stat().st_size == 0


async def test_async_lock_refuses_hardlink_before_initialization(tmp_path):
    root = tmp_path / "archive"
    target = hardlinked(root)
    with pytest.raises(OSError, match="Unsafe"):
        async with _exclusive_archive(root):
            pass
    assert target.read_bytes() == b""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO")
def test_nonregular_lock_is_refused(tmp_path):
    os.mkfifo(tmp_path / ".suno-sync.lock")
    with pytest.raises(OSError, match="Unsafe"), _exclusive_archive_sync(tmp_path):
        pass


def test_swapped_lock_path_is_rejected_before_writing(tmp_path, monkeypatch):
    target = tmp_path / ".suno-sync.lock"
    target.write_bytes(b"")
    real = os.open

    def swap(path, *args, **kwargs):
        fd = real(path, *args, **kwargs)
        if Path(path) == target:
            target.rename(tmp_path / "original")
            target.write_bytes(b"")
        return fd

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(OSError, match="Unsafe"), _exclusive_archive_sync(tmp_path):
        pass
    assert (tmp_path / "original").read_bytes() == b"" and target.read_bytes() == b""
