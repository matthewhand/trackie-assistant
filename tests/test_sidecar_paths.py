"""Id sidecar on every save path of the per-candidate downloader, plus rotation.

``_try_download_candidate`` owns the saving (plain, ``--extract-audio`` remux,
audio-only mp4 fallback); each path must leave the ``<stem>.json`` sidecar, a
failed extraction must leave none, a track already on disk is backfilled, and
the sidecar skip-by-id must not upset the rotation bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import _download_single, download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.rotate import STATE_FILENAME
from suno_dl.sidecar import build_sidecar, known_ids, read_sidecar, sidecar_path, write_sidecar

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
M4A = f"https://cdn1.suno.ai/{CLIP}.m4a"
MP3 = f"https://cdn1.suno.ai/{CLIP}.mp3"
MP4 = f"https://cdn1.suno.ai/{CLIP}.mp4"
_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(*urls: str, clip_id: str = CLIP, title: str = "Song", **kw) -> TrackInfo:
    return TrackInfo(
        id=clip_id,
        title=title,
        page_url=f"https://suno.com/song/{clip_id}",
        audio_url=urls[0],
        media_urls=list(urls),
        **kw,
    )


def _fetcher(handler=None) -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda r: httpx.Response(200, content=_VALID_MEDIA))
        )
    )
    return f


def _stats() -> dict[str, int]:
    return {"total": 1, "downloaded": 0, "skipped_exists": 0, "failed": 0, "skipped_video": 0}


async def _single(track, tmp_path, fetcher, stats=None, **kw):
    stats = stats if stats is not None else _stats()
    result = await _download_single(
        track, tmp_path / "Song", tmp_path, fetcher, asyncio.Semaphore(1), stats, None, **kw
    )
    await fetcher.__aexit__()
    return result, stats


def _side(tmp_path: Path) -> dict:
    data = read_sidecar(tmp_path / "Song.json")
    assert data is not None, "no valid sidecar written"
    return data


# --- every save path writes the sidecar --------------------------------------


@pytest.mark.parametrize("audio_only", [False, True])
async def test_m4a_save_writes_sidecar(tmp_path: Path, audio_only: bool) -> None:
    result, stats = await _single(
        _track(M4A), tmp_path, _fetcher(), audio_only=audio_only, covers=False, sidecar=True
    )
    assert result == "downloaded" and stats["sidecars"] == 1
    data = _side(tmp_path)
    assert data["id"] == CLIP and data["saved_files"] == ["Song.m4a"]
    assert data["source"] == "download"


@pytest.mark.parametrize("audio_only", [False, True])
async def test_plain_mp3_save_writes_sidecar(tmp_path: Path, audio_only: bool) -> None:
    result, _ = await _single(
        _track(MP3), tmp_path, _fetcher(), audio_only=audio_only, sidecar=True
    )
    assert result == "downloaded"
    assert _side(tmp_path)["saved_files"] == ["Song.mp3"]


async def test_plain_mp4_save_writes_sidecar(tmp_path: Path) -> None:
    result, _ = await _single(_track(MP4), tmp_path, _fetcher(), sidecar=True)
    assert result == "downloaded"
    assert _side(tmp_path)["saved_files"] == ["Song.mp4"]


async def test_cover_is_listed_in_the_sidecar(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "cover" in req.url.path or req.url.path.endswith((".jpeg", ".jpg", ".png")):
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * 2048)
        return httpx.Response(200, content=_VALID_MEDIA)

    result, _ = await _single(
        _track(M4A, cover_url=f"https://cdn2.suno.ai/image_large_{CLIP}.jpeg"),
        tmp_path,
        _fetcher(handler),
        covers=True,
        sidecar=True,
    )
    assert result == "downloaded"
    saved = _side(tmp_path)["saved_files"]
    assert "Song.m4a" in saved
    assert all((tmp_path / name).is_file() for name in saved)


async def test_mp4_extract_path_writes_sidecar_for_the_m4a(tmp_path: Path, monkeypatch) -> None:
    from suno_dl.downloader import _try_download_candidate

    async def fake_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(b"extracted-audio")
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", fake_extract)
    f = _fetcher()
    stats = _stats()
    result = await _try_download_candidate(
        MP4,
        _track(MP4),
        tmp_path / "Song",
        tmp_path,
        f,
        stats,
        None,
        "h",
        False,
        extract_audio=True,
        sidecar=True,
    )
    await f.__aexit__()
    assert result == "downloaded" and stats["sidecars"] == 1
    assert not (tmp_path / "Song.mp4").exists()
    # The mp4 was removed, so the sidecar must name the m4a that is really there.
    assert _side(tmp_path)["saved_files"] == ["Song.m4a"]


async def test_audio_only_mp4_fallback_writes_sidecar(tmp_path: Path, monkeypatch) -> None:
    async def fake_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(b"extracted-audio")
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", fake_extract)

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(403)

    result, _ = await _single(
        _track(M4A, MP3, MP4),
        tmp_path,
        _fetcher(handler),
        audio_only=True,
        extract_audio=True,
        sidecar=True,
    )
    assert result == "downloaded"
    assert _side(tmp_path)["saved_files"] == ["Song.m4a"]


async def test_failed_extraction_leaves_no_sidecar(tmp_path: Path, monkeypatch) -> None:
    async def failing_extract(src: Path, dest: Path) -> bool:
        return False

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", failing_extract)

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(403)

    result, stats = await _single(
        _track(M4A, MP4),
        tmp_path,
        _fetcher(handler),
        audio_only=True,
        extract_audio=True,
        sidecar=True,
    )
    assert result == "failed" and stats.get("sidecars", 0) == 0
    assert list(tmp_path.glob("*.json")) == [] and not (tmp_path / "Song.mp4").exists()


async def test_failed_download_leaves_no_sidecar(tmp_path: Path) -> None:
    result, _ = await _single(
        _track(M4A), tmp_path, _fetcher(lambda r: httpx.Response(404)), sidecar=True
    )
    assert result == "failed"
    assert list(tmp_path.glob("*.json")) == []


async def test_sidecar_is_off_unless_asked_on_the_candidate_path(tmp_path: Path) -> None:
    result, _ = await _single(_track(M4A), tmp_path, _fetcher())
    assert result == "downloaded"
    assert list(tmp_path.glob("*.json")) == []


# --- backfill for tracks already on disk -------------------------------------


@pytest.mark.parametrize("audio_only", [False, True])
async def test_unowned_track_on_disk_is_not_backfilled(tmp_path: Path, audio_only: bool) -> None:
    (tmp_path / "Song.m4a").write_bytes(_VALID_MEDIA)

    def handler(req):
        raise AssertionError("an existing track must not be fetched")

    result, stats = await _single(
        _track(M4A), tmp_path, _fetcher(handler), audio_only=audio_only, sidecar=True
    )
    assert result == "failed" and stats.get("sidecars", 0) == 0
    assert (tmp_path / "Song.m4a").is_file()
    assert not (tmp_path / "Song.json").exists()


async def test_backfill_never_overwrites_an_existing_sidecar(tmp_path: Path) -> None:
    (tmp_path / "Song.m4a").write_bytes(_VALID_MEDIA)
    mine = build_sidecar(_track(M4A, title="Original"), handle="h", saved_files=["Song.m4a"])
    write_sidecar(tmp_path / "Song.json", mine)
    before = (tmp_path / "Song.json").read_bytes()
    result, stats = await _single(_track(M4A), tmp_path, _fetcher(), sidecar=True)
    assert result == "failed" and stats.get("sidecars", 0) == 0
    assert (tmp_path / "Song.json").read_bytes() == before


# --- skip by clip id, with the rest of #93's machinery -----------------------


def _clip(n: int) -> str:
    return f"a1b2c3d4-1111-4111-8111-{n:012d}"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_skip_by_id_does_not_break_rotation_bookkeeping(tmp_path: Path) -> None:
    """An id-skipped track has no task; rotation state must still line up per track."""
    old = tmp_path / "Renamed Track.m4a"
    old.write_bytes(_VALID_MEDIA)
    have = _track(M4A, clip_id=_clip(1), title="Old Title")
    write_sidecar(sidecar_path(old), build_sidecar(have, handle="drinko", saved_files=[old.name]))
    new_a = _track(
        f"https://cdn1.suno.ai/{_clip(2)}.m4a",
        clip_id=_clip(2),
        title="New A",
        created_at="2026-10-02T00:00:00Z",
    )
    new_b = _track(
        f"https://cdn1.suno.ai/{_clip(3)}.m4a",
        clip_id=_clip(3),
        title="New B",
        created_at="2026-10-03T00:00:00Z",
    )
    f = _fetcher()
    stats = await download_tracks(
        [new_a, have, new_b], f, 1, tmp_path, "{title}", "drinko", max_tracks=50, rotate=True
    )
    await f.__aexit__()
    assert stats["downloaded"] == 2 and stats["skipped_exists"] == 1
    state = json.loads((tmp_path / STATE_FILENAME).read_text())
    assert state["tracks"]["New A"]["id"] == _clip(2)
    assert state["tracks"]["New B"]["id"] == _clip(3)
    assert "Old Title" not in state["tracks"] and "Renamed Track" not in state["tracks"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_rotation_prunes_the_track_and_its_id_sidecar_end_to_end(tmp_path: Path) -> None:
    tracks = [
        _track(
            f"https://cdn1.suno.ai/{_clip(n)}.m4a",
            clip_id=_clip(n),
            title=f"T{n}",
            created_at=f"2026-10-0{n}T00:00:00Z",
        )
        for n in (1, 2, 3)
    ]
    f = _fetcher()
    stats = await download_tracks(
        tracks, f, 1, tmp_path, "{title}", "drinko", max_tracks=2, rotate=True
    )
    await f.__aexit__()
    assert stats["rotated_tracks"] == 1
    assert not (tmp_path / "T1.m4a").exists() and not (tmp_path / "T1.json").exists()
    assert (tmp_path / "T2.json").exists() and (tmp_path / "T3.json").exists()
    assert set(known_ids(tmp_path)) == {_clip(2), _clip(3)}
    # ...and the pruned id is not fetched again on the next sync.
    f2 = _fetcher(lambda r: pytest.fail("pruned track was fetched again"))
    again = await download_tracks(
        tracks, f2, 1, tmp_path, "{title}", "drinko", max_tracks=2, rotate=True
    )
    await f2.__aexit__()
    assert again["downloaded"] == 0 and again["skipped_rotated"] == 1
