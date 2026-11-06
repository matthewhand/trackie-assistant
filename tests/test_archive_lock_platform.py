"""Synthetic cross-platform branch regression; no actual process or file locks."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from suno_dl import archive_lock


class _FictionalFile:
    def seek(self, position: int) -> int:
        assert position == 0
        return 0

    def fileno(self) -> int:
        return 41


def test_windows_lock_branch_still_uses_msvcrt(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    module = ModuleType("msvcrt")
    module.LK_NBLCK = 2
    module.LK_UNLCK = 0

    def locking(fd: int, mode: int, amount: int) -> None:
        calls.append((fd, mode, amount))

    module.locking = locking
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", module)
        assert archive_lock._try_lock(_FictionalFile()) is True
        archive_lock._unlock(_FictionalFile())

    assert calls == [(41, 2, 1), (41, 0, 1)]


async def test_windows_writer_refuses_before_archive_mutation(tmp_path, monkeypatch):
    from suno_dl.download_intent import UnsafeDownloadIntent

    calls = []

    @archive_lock.archive_locked_if_capped
    async def writer(output_dir, dry_run=False):
        calls.append("mutated")
        (output_dir / "audio.m4a").write_bytes(b"media")

    monkeypatch.setattr(archive_lock.sys, "platform", "win32")
    with pytest.raises(UnsafeDownloadIntent, match="Windows Trackie archive writes"):
        await writer(tmp_path)
    assert calls == []
    assert not (tmp_path / "audio.m4a").exists()
    assert list(tmp_path.iterdir()) == []


async def test_nested_writer_reuses_same_task_lock_but_not_child_task(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    calls = []

    @archive_lock.archive_locked_if_capped
    async def inner(output_dir, dry_run=False):
        calls.append("inner")

    @archive_lock.archive_locked_if_capped
    async def outer(output_dir, dry_run=False):
        await inner(output_dir)
        with pytest.raises(TimeoutError):
            await asyncio.create_task(inner(output_dir))

    await outer(tmp_path)
    assert calls == ["inner"]


async def test_windows_dry_run_does_not_create_missing_archive(tmp_path, monkeypatch):
    from suno_dl.downloader import download_tracks

    missing = tmp_path / "new-library"
    monkeypatch.setattr(archive_lock.sys, "platform", "win32")
    stats = await download_tracks([], None, 1, missing, "{artist}_{title}", "owner", dry_run=True)
    assert stats["dry_run"] == 0
    assert not missing.exists()
