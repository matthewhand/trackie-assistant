"""Explicit, verified replacement of an existing recording (#167)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.sidecar import build_sidecar, write_sidecar

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
JUNK = b"<html>encrypted payload</html>" + b"\x00" * 2048


def _track() -> TrackInfo:
    url = f"https://cdn1.suno.ai/{CLIP}.m4a"
    return TrackInfo(
        id=CLIP,
        title="Old",
        artist="SmileDJ",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=url,
        media_urls=[url],
    )


def _seed_legacy(root: Path, payload: bytes = JUNK, ext: str = ".m4a") -> Path:
    folder = root / "drinko"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / f"drinko_Old{ext}"
    media.write_bytes(payload)
    write_sidecar(
        folder / "drinko_Old.json",
        build_sidecar(_track(), handle="drinko", saved_files=[media.name]),
    )
    return media


def _fetcher(handler=None) -> Fetcher:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler or refuse))
    return fetcher


def _serve(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=MEDIA)


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_replaces_corrupt_recording_in_place(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _serve(request)

    stats = await download_tracks(
        [_track()],
        _fetcher(handler),
        1,
        tmp_path,
        "{title}",
        "drinko",
        subdir="playlist",
        force_redownload=True,
    )
    assert calls == [f"https://cdn1.suno.ai/{CLIP}.m4a"]
    assert stats["downloaded"] == 1
    assert stats["replaced"] == 1
    assert stats["invalid_existing"] == 1
    assert media.read_bytes() == MEDIA
    assert sorted(tmp_path.rglob("*.m4a")) == [media], "no second copy under the playlist folder"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_refuses_unverified_format_change(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path, payload=MEDIA, ext=".mp3")
    stats = await download_tracks(
        [_track()],
        _fetcher(_serve),
        1,
        tmp_path,
        "{title}",
        "drinko",
        force_redownload=True,
    )
    assert stats["failed"] == 1 and stats["replaced"] == 0
    assert media.read_bytes() == MEDIA
    assert not (tmp_path / "drinko" / "drinko_Old.m4a").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_existing_recording_is_untouched_without_the_flag(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)
    stats = await download_tracks(
        [_track()],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        subdir="playlist",
    )
    assert stats["skipped_exists"] == 1
    assert stats["replaced"] == 0
    assert media.read_bytes() == JUNK


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_refuses_unowned_file_without_sidecar(tmp_path: Path) -> None:
    media = tmp_path / "Old.m4a"
    media.write_bytes(JUNK)
    stats = await download_tracks(
        [_track()],
        _fetcher(_serve),
        1,
        tmp_path,
        "{title}",
        "drinko",
        sidecar=False,
        force_redownload=True,
    )
    assert stats["failed"] == 1 and stats["replaced"] == 0
    assert media.read_bytes() == JUNK


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_dry_run_probes_without_writing(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)
    stats = await download_tracks(
        [_track()],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        dry_run=True,
        force_redownload=True,
    )
    assert stats["dry_run"] == 1
    assert media.read_bytes() == JUNK


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_failed_force_transfer_leaves_the_original_playable(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)
    stats = await download_tracks(
        [_track()],
        _fetcher(lambda req: httpx.Response(500, content=b"nope")),
        1,
        tmp_path,
        "{title}",
        "drinko",
        force_redownload=True,
    )
    assert stats["downloaded"] == 0
    assert stats["replaced"] == 0
    assert stats["failed"] == 1
    assert media.read_bytes() == JUNK
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_never_removes_unlisted_same_name_media(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)
    unrelated = media.with_suffix(".mp3")
    unrelated.write_bytes(b"unrelated recording")
    stats = await download_tracks(
        [_track()], _fetcher(_serve), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["replaced"] == 1 and media.read_bytes() == MEDIA
    assert unrelated.read_bytes() == b"unrelated recording"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_refuses_unlisted_replacement_target(tmp_path: Path) -> None:
    owned = _seed_legacy(tmp_path, ext=".mp3")
    unrelated = owned.with_suffix(".m4a")
    unrelated.write_bytes(b"unrelated recording")
    stats = await download_tracks(
        [_track()], _fetcher(_serve), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["failed"] == 1 and stats["replaced"] == 0
    assert unrelated.read_bytes() == b"unrelated recording"
    assert owned.read_bytes() == JUNK


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_refuses_file_changed_during_transfer(tmp_path: Path) -> None:
    media = _seed_legacy(tmp_path)

    def handler(request):
        media.write_bytes(b"changed by another writer")
        return _serve(request)

    stats = await download_tracks(
        [_track()], _fetcher(handler), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["failed"] == 1 and stats["replaced"] == 0
    assert media.read_bytes() == b"changed by another writer"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_refuses_file_replaced_after_index_scan(tmp_path: Path, monkeypatch) -> None:
    import suno_dl.downloader as module

    media = _seed_legacy(tmp_path)
    other = media.with_name("otherfile")
    other.write_bytes(b"independent user file")

    def change_after_index(path):
        other.replace(media)
        return None

    monkeypatch.setattr(module, "probe_media", change_after_index)
    stats = await download_tracks(
        [_track()], _fetcher(_serve), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["failed"] == 1 and stats["replaced"] == 0
    assert media.read_bytes() == b"independent user file"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_preserves_verified_companion_sidecar_entries(tmp_path: Path) -> None:
    import json

    media = _seed_legacy(tmp_path)
    cover = media.with_suffix(".cover.jpeg")
    cover.write_bytes(b"cover image")
    side = media.with_suffix(".json")
    write_sidecar(
        side,
        build_sidecar(_track(), handle="drinko", saved_files=[media.name, cover.name]),
        overwrite=True,
    )
    stats = await download_tracks(
        [_track()], _fetcher(_serve), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["replaced"] == 1
    assert cover.read_bytes() == b"cover image"
    assert cover.name in json.loads(side.read_text())["saved_files"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_force_does_not_claim_missing_companion_created_during_transfer(
    tmp_path: Path,
) -> None:
    import json

    media = _seed_legacy(tmp_path)
    side = media.with_suffix(".json")
    unrelated = media.with_suffix(".mp3")
    write_sidecar(
        side,
        build_sidecar(_track(), handle="drinko", saved_files=[media.name, unrelated.name]),
        overwrite=True,
    )

    def handler(request):
        unrelated.write_bytes(b"unrelated")
        return _serve(request)

    stats = await download_tracks(
        [_track()], _fetcher(handler), 1, tmp_path, "{title}", "drinko", force_redownload=True
    )
    assert stats["replaced"] == 1
    assert unrelated.read_bytes() == b"unrelated"
    assert unrelated.name not in json.loads(side.read_text())["saved_files"]
