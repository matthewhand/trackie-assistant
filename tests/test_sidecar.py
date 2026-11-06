"""Sidecar: written beside each track, original title kept, skip by clip id."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.sidecar import (
    build_sidecar,
    known_ids,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
OTHER = "b2c3d4e5-2222-4222-8222-222222222222"
_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
TITLE = "Überwanker: Part 1/2? (feat. Alice & Bob) [COPY]\u00a0[nsfw]"


def _track(clip_id: str = CLIP, title: str = TITLE, **kw) -> TrackInfo:
    url = f"https://cdn1.suno.ai/{clip_id}.m4a"
    base = {
        "id": clip_id,
        "title": title,
        "page_url": f"https://suno.com/song/{clip_id}",
        "audio_url": url,
        "media_urls": [url],
        "created_at": "2026-10-09T01:02:03.000Z",
        "model_name": "chirp-fenix",
        "model_version": "v5",
        "tags": "chipfunk, 8-bit",
        "duration_sec": 213,
    }
    base.update(kw)
    return TrackInfo(**base)


def _fetcher() -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=_VALID_MEDIA))
    )
    return f


def test_sidecar_path_replaces_only_the_media_extension() -> None:
    assert sidecar_path(Path("/a/drinko_Song.m4a")).name == "drinko_Song.json"
    assert sidecar_path(Path("/a/drinko_Song (feat. X).m4a")).name == "drinko_Song (feat. X).json"
    # A bare stem with a mid-name period is not mistaken for an extension.
    assert sidecar_path(Path("/a/drinko_Song (feat. X)")).name == "drinko_Song (feat. X).json"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_written_beside_file_with_original_title(tmp_path: Path) -> None:
    f = _fetcher()
    stats = await download_tracks([_track()], f, 1, tmp_path, "{handle}_{title}", "drinko")
    await f.__aexit__()
    assert stats["downloaded"] == 1 and stats["sidecars"] == 1
    audio = next(tmp_path.glob("*.m4a"))
    side = sidecar_path(audio)
    assert side.exists() and side.parent == audio.parent
    assert side.stem == audio.stem
    data = json.loads(side.read_text(encoding="utf-8"))
    assert data["id"] == CLIP
    assert data["url"] == f"https://suno.com/song/{CLIP}"
    # The filename is sanitised, the sidecar keeps the title exactly as Suno had it.
    assert data["title"] == TITLE
    assert ":" not in audio.name and "/" not in audio.name
    assert data["handle"] == "drinko"
    assert data["created_at"] == "2026-10-09T01:02:03.000Z"
    assert data["model_name"] == "chirp-fenix" and data["model_version"] == "v5"
    assert data["duration_sec"] == 213 and data["tags"] == "chipfunk, 8-bit"
    assert data["is_public"] is True
    assert data["saved_files"] == [audio.name]
    assert data["downloaded_at"].endswith("Z") and data["suno_dl_version"]
    assert data["source"] == "download"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_holds_no_urls_or_secrets(tmp_path: Path) -> None:
    signed = f"https://cdn1.suno.ai/{CLIP}.m4a?Signature=SECRET&Key-Pair-Id=K"
    f = _fetcher()
    await download_tracks(
        [_track(audio_url=signed, media_urls=[signed])], f, 1, tmp_path, "{title}", "h"
    )
    await f.__aexit__()
    text = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "SECRET" not in text and "Signature" not in text and "cdn1" not in text


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_no_sidecar_flag_disables_it(tmp_path: Path) -> None:
    f = _fetcher()
    stats = await download_tracks([_track()], f, 1, tmp_path, "{title}", "h", sidecar=False)
    await f.__aexit__()
    assert stats["downloaded"] == 1 and stats["sidecars"] == 0
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_skip_by_id_when_filename_differs(tmp_path: Path) -> None:
    # An earlier run saved the clip under another name (old title / old sanitiser).
    old = tmp_path / "drinko_Old Title.m4a"
    old.write_bytes(_VALID_MEDIA)
    write_sidecar(
        sidecar_path(old),
        build_sidecar(_track(title="Old Title"), handle="drinko", saved_files=[old.name]),
    )
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=_VALID_MEDIA)

    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stats = await download_tracks(
        [_track(title="Brand New Title")], f, 1, tmp_path, "{handle}_{title}", "drinko"
    )
    await f.__aexit__()
    assert stats["skipped_exists"] == 1 and stats["downloaded"] == 0
    assert calls["n"] == 0
    assert [p.name for p in tmp_path.glob("*.m4a")] == [old.name]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_ignored_when_its_media_is_gone(tmp_path: Path) -> None:
    ghost = tmp_path / "drinko_Gone.m4a"
    write_sidecar(
        sidecar_path(ghost),
        build_sidecar(_track(), handle="drinko", saved_files=[ghost.name]),
    )
    assert known_ids(tmp_path) == {}
    f = _fetcher()
    stats = await download_tracks([_track()], f, 1, tmp_path, "{handle}_{title}", "drinko")
    await f.__aexit__()
    assert stats["downloaded"] == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_no_sidecar_still_uses_existing_identity(tmp_path: Path) -> None:
    old = tmp_path / "drinko_Old Title.m4a"
    old.write_bytes(_VALID_MEDIA)
    write_sidecar(
        sidecar_path(old), build_sidecar(_track(), handle="drinko", saved_files=[old.name])
    )
    f = _fetcher()
    stats = await download_tracks(
        [_track(title="New")], f, 1, tmp_path, "{handle}_{title}", "drinko", sidecar=False
    )
    await f.__aexit__()
    assert stats["skipped_exists"] == 1
    assert [p.name for p in tmp_path.glob("*.m4a")] == [old.name]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_skip_by_id_keeps_name_reserved_for_same_titled_clip(tmp_path: Path) -> None:
    """Skipping clip A by id must not let same-titled clip B land on A's file."""
    a = tmp_path / "drinko_Same.m4a"
    a.write_bytes(_VALID_MEDIA)
    write_sidecar(
        sidecar_path(a),
        build_sidecar(_track(title="Same"), handle="drinko", saved_files=[a.name]),
    )
    f = _fetcher()
    stats = await download_tracks(
        [_track(CLIP, "Same"), _track(OTHER, "Same")], f, 1, tmp_path, "{handle}_{title}", "drinko"
    )
    await f.__aexit__()
    assert stats["skipped_exists"] == 1 and stats["downloaded"] == 1
    assert (tmp_path / "drinko_Same_2.m4a").exists()
    assert read_sidecar(tmp_path / "drinko_Same_2.json")["id"] == OTHER  # type: ignore[index]
    assert read_sidecar(a.with_suffix(".json"))["id"] == CLIP  # type: ignore[index]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_existing_file_without_sidecar_is_not_guessed_or_overwritten(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "drinko_Song.m4a"
    audio.write_bytes(_VALID_MEDIA)
    f = _fetcher()
    stats = await download_tracks(
        [_track(title="Song")], f, 1, tmp_path, "{handle}_{title}", "drinko"
    )
    assert stats["failed"] == 1 and stats["sidecars"] == 0
    side = sidecar_path(audio)
    assert not side.exists() and audio.read_bytes() == _VALID_MEDIA
    side.write_text(
        json.dumps({"id": CLIP, "saved_files": [audio.name], "note": "hand edited"}),
        encoding="utf-8",
    )
    stats = await download_tracks(
        [_track(title="Song")], f, 1, tmp_path, "{handle}_{title}", "drinko"
    )
    await f.__aexit__()
    assert stats["sidecars"] == 0
    assert json.loads(side.read_text(encoding="utf-8"))["note"] == "hand edited"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_dry_run_writes_no_sidecar(tmp_path: Path) -> None:
    f = _fetcher()
    await download_tracks([_track()], f, 1, tmp_path, "{title}", "h", dry_run=True)
    await f.__aexit__()
    assert list(tmp_path.iterdir()) == []


def test_read_sidecar_rejects_foreign_json(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text('{"id": "nope"}')
    (tmp_path / "y.json").write_text("[1, 2]")
    (tmp_path / "z.json").write_text("not json")
    assert known_ids(tmp_path) == {}


def test_sidecar_write_does_not_follow_preplanted_temporary_symlink(tmp_path: Path) -> None:
    """#157: a predictable .json.part path must not overwrite another file."""
    archive = tmp_path / "library"
    archive.mkdir()
    victim = tmp_path / "outside-archive.txt"
    victim.write_text("PRIVATE SENTINEL — MUST STAY UNCHANGED", encoding="utf-8")
    path = archive / "Song.json"
    planted = archive / "Song.json.part"
    planted.symlink_to(victim)
    data = {"id": CLIP, "saved_files": ["Song.m4a"], "title": "Æsthaeth 🎵 e\u0301"}

    assert write_sidecar(path, data)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert victim.read_text(encoding="utf-8") == "PRIVATE SENTINEL — MUST STAY UNCHANGED"
    assert planted.is_symlink()
    assert planted.resolve() == victim


def test_sidecar_write_rejects_invalid_unicode_without_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "Song.json"
    assert not write_sidecar(path, {"title": "invalid\ud800surrogate"})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_sidecar_overwrite_false_does_not_change_existing_metadata(tmp_path: Path) -> None:
    path = tmp_path / "Song.json"
    assert write_sidecar(path, {"title": "Æsthaeth 🎵"})
    original = path.read_bytes()
    assert not write_sidecar(path, {"title": "malicious"}, overwrite=False)
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Song.json"]


def test_published_sidecar_stays_readable_beyond_the_writer(tmp_path: Path) -> None:
    """The 0600 exclusive temporary must not leak its mode to the sidecar."""
    import os

    if os.name != "posix":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "Song.json"
    assert write_sidecar(path, {"title": "Shared"})
    assert path.stat().st_mode & 0o444 == 0o444
