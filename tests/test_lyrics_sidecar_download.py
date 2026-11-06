"""--lyrics-sidecar reaches the file writer through the per-candidate download helper."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import _download_single
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
GOOD = f"https://cdn1.suno.ai/{CLIP}.m4a"
_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
LYRICS = "line one\nline two"


def _fetcher() -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=_VALID_MEDIA))
    )
    return f


def _track() -> TrackInfo:
    return TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
        lyrics=LYRICS,
    )


@pytest.mark.parametrize("audio_only", [False, True])
async def test_sidecar_written_when_requested(tmp_path: Path, audio_only: bool) -> None:
    f = _fetcher()
    stats = {"total": 1, "downloaded": 0, "skipped_exists": 0, "failed": 0, "skipped_video": 0}
    result = await _download_single(
        _track(),
        tmp_path / "Song",
        tmp_path,
        f,
        asyncio.Semaphore(1),
        stats,
        None,
        audio_only=audio_only,
        lyrics_sidecar="txt",
    )
    await f.__aexit__()
    assert result == "downloaded"
    assert (tmp_path / "Song.txt").read_text(encoding="utf-8").strip() == LYRICS


async def test_no_sidecar_by_default(tmp_path: Path) -> None:
    f = _fetcher()
    stats = {"total": 1, "downloaded": 0, "skipped_exists": 0, "failed": 0, "skipped_video": 0}
    result = await _download_single(
        _track(), tmp_path / "Song", tmp_path, f, asyncio.Semaphore(1), stats, None
    )
    await f.__aexit__()
    assert result == "downloaded"
    assert not (tmp_path / "Song.txt").exists()
    assert not (tmp_path / "Song.lrc").exists()
