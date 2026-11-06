"""Global clip-UUID library and playlist membership (#158).

Every case is offline: the index only reads sidecars and local media bytes, and
the downloader is driven through MockTransport so a media GET would fail the
test rather than silently succeed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.library import LibraryIndex, load_membership, probe_media, record_membership
from suno_dl.models import TrackInfo
from suno_dl.sidecar import build_sidecar, write_sidecar

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
OTHER = "b2c3d4e5-2222-4222-8222-222222222222"
MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(clip_id: str = CLIP, title: str = "Song") -> TrackInfo:
    url = f"https://cdn1.suno.ai/{clip_id}.m4a"
    return TrackInfo(
        id=clip_id,
        title=title,
        artist="SmileDJ",
        page_url=f"https://suno.com/song/{clip_id}",
        audio_url=url,
        media_urls=[url],
    )


def _seed(folder: Path, clip_id: str, stem: str, media: bytes = MEDIA) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{stem}.m4a"
    audio.write_bytes(media)
    assert write_sidecar(
        folder / f"{stem}.json",
        build_sidecar(_track(clip_id, stem), handle="drinko", saved_files=[audio.name]),
    )
    return audio


def _fetcher(handler=None) -> Fetcher:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected media request: {request.url}")

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler or refuse))
    return fetcher


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_one_recording_serves_two_playlists_without_a_transfer(tmp_path: Path) -> None:
    legacy = tmp_path / "drinko"
    _seed(legacy, CLIP, "drinko_Song")

    fetcher = _fetcher()
    first = await download_tracks(
        [_track()],
        fetcher,
        1,
        tmp_path,
        "{title}",
        "drinko",
        subdir="playlist-one",
        source="playlist-one",
    )
    second = await download_tracks(
        [_track()],
        fetcher,
        1,
        tmp_path,
        "{title}",
        "drinko",
        subdir="playlist-two",
        source="playlist-two",
    )
    assert first["skipped_exists"] == 1 and first["downloaded"] == 0
    assert second["skipped_exists"] == 1 and second["downloaded"] == 0

    media = sorted(tmp_path.rglob("*.m4a"))
    assert media == [legacy / "drinko_Song.m4a"], "a shared clip must not be duplicated"
    for playlist in ("playlist-one", "playlist-two"):
        assert not list((tmp_path / playlist).iterdir())

    membership = load_membership(tmp_path)
    assert membership["playlist-one"] == [CLIP]
    assert membership["playlist-two"] == [CLIP]


async def test_membership_records_discovery_order_and_is_idempotent(tmp_path: Path) -> None:
    ids = [CLIP, OTHER]
    path = record_membership(tmp_path, "playlist", ids)
    assert path is not None
    first = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    again = record_membership(tmp_path, "playlist", ids)
    assert again == path
    assert path.read_bytes() == first
    assert path.stat().st_mtime_ns == stamp
    assert load_membership(tmp_path)["playlist"] == ids
    data = json.loads(path.read_text())
    assert data["schema"] == 1


def test_corrupt_membership_is_derived_data_not_a_failure(tmp_path: Path) -> None:
    path = tmp_path / ".library" / "membership.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_membership(tmp_path) == {}
    record_membership(tmp_path, "playlist", [CLIP])
    assert load_membership(tmp_path) == {"playlist": [CLIP]}


def test_symlinked_and_truncated_media_are_not_indexed(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.m4a"
    outside.write_bytes(MEDIA)
    folder = tmp_path / "archive"
    folder.mkdir()
    (folder / "link.m4a").symlink_to(outside)
    (folder / "small.m4a").write_bytes(b"\x00" * 10)
    for name, listed in (("link", "link.m4a"), ("small", "small.m4a")):
        write_sidecar(
            folder / f"{name}.json",
            build_sidecar(_track(CLIP, name), handle="h", saved_files=[listed]),
        )
    (folder / "escape.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": OTHER,
                "title": "escape",
                "saved_files": ["../outside.m4a"],
            }
        ),
        encoding="utf-8",
    )
    index = LibraryIndex(tmp_path).scan()
    assert index.get(CLIP) is None
    assert index.get(OTHER) is None


def test_conflicting_claims_for_one_uuid_fail_closed(tmp_path: Path) -> None:
    _seed(tmp_path / "one", CLIP, "first")
    _seed(tmp_path / "two", CLIP, "second")
    index = LibraryIndex(tmp_path).scan()
    assert index.get(CLIP) is None
    assert CLIP in index.conflicts


def test_identical_claims_are_one_recording(tmp_path: Path) -> None:
    audio = _seed(tmp_path / "one", CLIP, "first")
    two = tmp_path / "two"
    two.mkdir()
    (two / "first.m4a").hardlink_to(audio)
    write_sidecar(
        two / "first.json",
        build_sidecar(_track(CLIP, "first"), handle="h", saved_files=["first.m4a"]),
    )
    index = LibraryIndex(tmp_path).scan()
    assert index.get(CLIP) is not None
    assert not index.conflicts


def test_probe_is_read_only_and_reports_container(tmp_path: Path) -> None:
    good = tmp_path / "ok.m4a"
    good.write_bytes(MEDIA)
    before = good.read_bytes()
    assert probe_media(good) == ""
    assert good.read_bytes() == before

    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"<html>nope</html>" + b"\x00" * 4096)
    assert probe_media(junk) == "unrecognised container"
    assert probe_media(tmp_path / "absent.m4a") == "missing"
