"""Size and time bounds on untrusted content (#157).

Every case is offline and synthetic: the cap must abort the transfer, the
read or the write — never allocate first and never damage existing files.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import MAX_COVER_BYTES
from suno_dl.errors import SunoError
from suno_dl.fetcher import MAX_RESPONSE_BYTES, Fetcher, read_capped
from suno_dl.models import MAX_LYRICS_CHARS, MAX_TAGS_CHARS, MAX_TITLE_CHARS, TrackInfo
from suno_dl.sidecar import MAX_SIDECAR_BYTES, read_sidecar, write_sidecar
from suno_dl.tagging import write_lyrics_sidecar

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"


def _fetcher(handler) -> Fetcher:
    """Fetcher wired to a mock transport, used directly (no ``async with``).

    ``Fetcher.__aenter__`` installs a *fresh* client, which would discard the
    mock and hit the real network, so the injected client is closed by the
    caller instead.
    """
    fetcher = Fetcher(timeout_sec=5.0, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetcher


async def test_oversized_html_response_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>" + b"a" * (MAX_RESPONSE_BYTES + 1024))

    fetcher = _fetcher(handler)
    try:
        with pytest.raises(SunoError, match="exceeds"):
            await fetcher.fetch_html("https://suno.com/song/" + CLIP)
    finally:
        await fetcher.client.aclose()


async def test_small_html_response_still_parses() -> None:
    payload = "<html><body>ok</body></html>"
    fetcher = _fetcher(lambda req: httpx.Response(200, content=payload.encode()))
    try:
        assert await fetcher.fetch_html("https://suno.com/@drinko") == payload
    finally:
        await fetcher.client.aclose()


async def test_oversized_json_feed_is_refused() -> None:
    from suno_dl.discovery import _fetch_feed_page

    body = json.dumps({"clips": ["x" * 1024] * (MAX_RESPONSE_BYTES // 1024 + 16)}).encode()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    )
    try:
        with pytest.raises(SunoError, match="exceeds"):
            await _fetch_feed_page(client, "drinko", 1)
    finally:
        await client.aclose()


async def test_feed_json_must_be_an_object() -> None:
    from suno_dl.discovery import _fetch_feed_page

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"[1, 2, 3]"))
    )
    try:
        assert await _fetch_feed_page(client, "drinko", 1) == {}
    finally:
        await client.aclose()


async def test_read_capped_aborts_before_buffering_everything() -> None:
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    response = httpx.Response(200, content=body)
    with pytest.raises(SunoError, match="exceeds"):
        await read_capped(response)


def test_oversized_sidecar_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "Song.json"
    path.write_text(
        json.dumps(
            {"schema": 1, "id": CLIP, "saved_files": ["Song.m4a"], "pad": "x" * MAX_SIDECAR_BYTES}
        ),
        encoding="utf-8",
    )
    assert read_sidecar(path) is None


def test_oversized_lyrics_sidecar_is_refused(tmp_path: Path) -> None:
    audio = tmp_path / "Song.m4a"
    audio.write_bytes(b"\x00" * 2048)
    assert write_lyrics_sidecar(audio, "x" * (MAX_LYRICS_CHARS * 4), "lrc") is None
    assert not (tmp_path / "Song.lrc").exists()


async def test_oversized_cover_transfer_is_dropped(tmp_path: Path) -> None:
    from suno_dl.downloader import _download_cover

    cover_url = f"https://cdn1.suno.ai/image_{CLIP}.jpeg"
    track = TrackInfo(
        id=CLIP,
        title="t",
        page_url=f"https://suno.com/song/{CLIP}",
        cover_url=cover_url,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_COVER_BYTES + 4096))

    fetcher = _fetcher(handler)
    await _download_cover(track, tmp_path / "Song", tmp_path, fetcher)
    assert not list(tmp_path.glob("*.cover.*")), "oversized cover must not be published"


def test_hostile_track_fields_are_bounded() -> None:
    track = TrackInfo(
        id=CLIP,
        title="t" * (MAX_TITLE_CHARS * 5),
        page_url=f"https://suno.com/song/{CLIP}",
        lyrics="l" * (MAX_LYRICS_CHARS * 3),
        tags="g" * (MAX_TAGS_CHARS * 3),
        duration_sec=-1,
        play_count=10**12,
    )
    assert len(track.title) == MAX_TITLE_CHARS
    assert len(track.lyrics or "") == MAX_LYRICS_CHARS
    assert len(track.tags or "") == MAX_TAGS_CHARS
    assert track.duration_sec is None
    assert track.play_count is None


def test_legitimate_values_survive_bounds() -> None:
    track = TrackInfo(
        id=CLIP,
        title="Überwanker: Part 1/2? (feat. Alice)",
        page_url=f"https://suno.com/song/{CLIP}",
        lyrics="two verses\nof real lyrics",
        tags="chipfunk, 8-bit",
        duration_sec=213,
        play_count=12,
    )
    assert track.title == "Überwanker: Part 1/2? (feat. Alice)"
    assert track.duration_sec == 213
    assert track.play_count == 12


def test_write_sidecar_still_round_trips(tmp_path: Path) -> None:
    data = {"schema": 1, "id": CLIP, "title": "ok", "saved_files": ["Song.m4a"]}
    assert write_sidecar(tmp_path / "Song.json", data)
    assert read_sidecar(tmp_path / "Song.json") == data
