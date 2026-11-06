"""Synthetic regression checks for paired-media caps and archive-wide locking.

No Suno calls, browser, installs, production files, or background services.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from suno_dl.archive_lock import _exclusive_archive
from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.rotate import archive_usage
from suno_dl.sidecar import build_sidecar, write_sidecar

IDS = (
    "a1b2c3d4-1111-4111-8111-111111111111",
    "a1b2c3d4-1111-4111-8111-222222222222",
)
PAYLOAD = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"X" * (350000 - 32)


def _track(index: int, *, covers: bool = False) -> TrackInfo:
    clip = IDS[index]
    return TrackInfo(
        id=clip,
        title=f"Budget Song {index}",
        handle=None,
        page_url=f"https://suno.com/song/{clip}",
        audio_url=f"https://cdn1.suno.ai/{clip}.m4a",
        media_urls=[
            f"https://cdn1.suno.ai/{clip}.m4a",
            f"https://cdn1.suno.ai/{clip}.mp4",
        ],
        cover_url=f"https://cdn1.suno.ai/{clip}.jpg" if covers else None,
    )


def _fetcher(handler) -> Fetcher:
    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetcher


async def _fake_remux(src: Path, dest: Path) -> bool:
    dest.write_bytes(src.read_bytes())
    return True


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("cover_length,expected_cover", [(800000, False), (95000, True)])
async def test_cover_cannot_bypass_video_pair_byte_cap(
    tmp_path: Path,
    cover_length: int,
    expected_cover: bool,
) -> None:
    seen = []
    image = b"\xff\xd8\xff" + b"I" * (cover_length - 3)

    def handler(request):
        seen.append(str(request.url))
        payload = image if ".jpg" in str(request.url) else PAYLOAD[:220000]
        return httpx.Response(200, content=payload)

    async def remux(src: Path, dest: Path) -> bool:
        dest.write_bytes(src.read_bytes())
        return True

    f = _fetcher(handler)
    try:
        with (
            patch("suno_dl.downloader._extract_audio_from_mp4", remux),
            patch("suno_dl.downloader._write_tags", lambda *_a, **_k: None),
        ):
            stats = await download_tracks(
                [_track(0, covers=True)],
                f,
                1,
                tmp_path,
                "{title}",
                "h",
                keep_video=True,
                extract_audio=True,
                sidecar=True,
                covers=True,
                max_disk_mb=1,
                rotate=False,
                min_free_mb=0,
            )
        assert stats["downloaded"] == 1
        assert (tmp_path / "Budget Song 0.mp4").is_file()
        assert (tmp_path / "Budget Song 0.m4a").is_file()
        assert (tmp_path / "Budget Song 0.cover.jpg").exists() is expected_cover
        assert archive_usage(tmp_path)[1] <= 1024 * 1024
        assert any(".jpg" in url for url in seen)
        assert not list(tmp_path.glob("*.part"))
    finally:
        await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_two_concurrent_sync_calls_use_one_archive_budget(tmp_path: Path) -> None:
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=PAYLOAD)

    fetchers = [_fetcher(handler), _fetcher(handler)]
    try:
        with (
            patch("suno_dl.downloader._extract_audio_from_mp4", _fake_remux),
            patch("suno_dl.downloader._write_tags", lambda *_a, **_k: None),
        ):
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        download_tracks(
                            [_track(i)],
                            fetchers[i],
                            1,
                            tmp_path,
                            "{title}",
                            "h",
                            keep_video=True,
                            extract_audio=True,
                            sidecar=True,
                            max_disk_mb=1,
                            rotate=False,
                            min_free_mb=0,
                        )
                        for i in range(2)
                    )
                ),
                timeout=10,
            )
        assert sorted(row["downloaded"] for row in results) == [0, 1]
        assert sorted(row["skipped_capped"] for row in results) == [0, 1]
        # The second run is allowed to inspect its URL, but the streaming
        # guard aborts its oversized transfer without saving either file.
        assert len(calls) == 2
        assert archive_usage(tmp_path)[1] <= 1024 * 1024
        assert archive_usage(tmp_path)[0] == 1
        assert (tmp_path / ".suno-sync.lock").is_file()
    finally:
        for fetcher in fetchers:
            await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_enrichment_does_not_spend_existing_track_slot(tmp_path: Path) -> None:
    existing_track = _track(0)
    existing_m4a = tmp_path / "Budget Song 0.m4a"
    existing_m4a.write_bytes(PAYLOAD[:3000])
    assert write_sidecar(
        tmp_path / "Budget Song 0.json",
        build_sidecar(existing_track, handle="h", saved_files=[existing_m4a.name]),
    )
    before = (existing_m4a.read_bytes(), existing_m4a.stat().st_mtime_ns)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD[:200000])

    fetcher = _fetcher(handler)
    try:
        with (
            patch("suno_dl.downloader._extract_audio_from_mp4", _fake_remux),
            patch("suno_dl.downloader._write_tags", lambda *_a, **_k: None),
        ):
            result = await download_tracks(
                [existing_track, _track(1)],
                fetcher,
                2,
                tmp_path,
                "{title}",
                "h",
                keep_video=True,
                extract_audio=True,
                max_tracks=2,
                sidecar=True,
                min_free_mb=0,
            )
        assert result["downloaded"] == 2
        assert result["skipped_capped"] == 0
        assert archive_usage(tmp_path)[0] == 2
        assert (existing_m4a.read_bytes(), existing_m4a.stat().st_mtime_ns) == before
        assert len(calls) == 2
    finally:
        await fetcher.__aexit__()


async def test_cross_process_lock_waits_and_recovers(tmp_path: Path) -> None:
    """A second Python process must hold this root against our in-process sync."""
    script = (
        "import asyncio,sys;"
        "from pathlib import Path;"
        "from suno_dl.archive_lock import _exclusive_archive;"
        "async def main():\n"
        " async with _exclusive_archive(Path(sys.argv[1])):\n"
        "  print('LOCKED',flush=True)\n"
        "  await asyncio.sleep(0.8)\n"
        "asyncio.run(main())"
    )
    # Compound code defines an async function; avoid semicolon before compound statement.
    script = script.replace(";async def main():", "\nasync def main():")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert line.strip() == b"LOCKED", line
        start = time.monotonic()
        async with _exclusive_archive(tmp_path):
            elapsed = time.monotonic() - start
        assert elapsed >= 0.4, elapsed
        await asyncio.wait_for(proc.wait(), timeout=5)
        assert proc.returncode == 0
        assert (tmp_path / ".suno-sync.lock").exists()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_cancelled_lock_wait_releases_after_owner_exits(tmp_path: Path) -> None:
    async with _exclusive_archive(tmp_path):
        waiting = asyncio.create_task(_wait_archive(tmp_path))
        await asyncio.sleep(0.12)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    await asyncio.wait_for(_wait_archive(tmp_path), timeout=2)


async def _wait_archive(path: Path) -> None:
    async with _exclusive_archive(path):
        pass
