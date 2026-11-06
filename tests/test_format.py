"""Tests for --format media preference (#13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from suno_dl.downloader import _media_candidates, download_tracks
from suno_dl.models import TrackInfo

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"


def _track(urls: list[str]) -> TrackInfo:
    return TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=urls[0] if urls else None,
        media_urls=urls,
    )


def test_preferred_format_reorders() -> None:
    urls = [
        f"https://cdn1.suno.ai/{CLIP}.mp3",
        f"https://cdn1.suno.ai/{CLIP}.m4a",
    ]
    default = _media_candidates(_track(urls))
    preferred = _media_candidates(_track(urls), preferred="m4a")
    assert preferred[0].endswith(".m4a")
    assert set(default) == set(preferred)  # no candidates dropped


def test_preferred_format_missing_falls_back() -> None:
    urls = [f"https://cdn1.suno.ai/{CLIP}.mp3"]
    out = _media_candidates(_track(urls), preferred="wav")
    assert out == urls  # nothing matches, but order preserved


def test_preferred_format_ignores_query_string() -> None:
    urls = [f"https://cdn1.suno.ai/{CLIP}.m4a?sig=x"]
    out = _media_candidates(_track(urls), preferred="m4a")
    assert out[0].endswith("?sig=x")


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_download_prefers_format(tmp_path: Path) -> None:
    import httpx

    from suno_dl.fetcher import Fetcher

    picked: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        picked.append(str(req.url))
        return httpx.Response(
            200,
            content=b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 4096,
        )

    f = Fetcher(1.0, 0)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    urls = [
        f"https://cdn1.suno.ai/{CLIP}.mp3",
        f"https://cdn1.suno.ai/{CLIP}.m4a",
    ]
    await download_tracks([_track(urls)], f, 1, tmp_path, "{title}", "h", preferred_format="m4a")
    assert picked and picked[0].endswith(".m4a")
    assert (tmp_path / "Song.m4a").exists()
    await f.__aexit__()
