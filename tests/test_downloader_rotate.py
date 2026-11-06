"""Tests for archive caps and rotation in the downloader (#93).

Covers the behaviour from the issue end to end through ``download_tracks``:
count cap, size cap, rotate on/off, sidecar cleanup, no re-download of pruned
tracks, and the destructive-path guards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.rotate import MEGABYTE, STATE_FILENAME

# Valid media bytes for fake CDN responses (MP4 'ftyp' header + padding).
_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(clip_id: str, title: str, created_at: str | None = None) -> TrackInfo:
    url = f"https://cdn1.suno.ai/{clip_id}.m4a"
    return TrackInfo(
        id=clip_id,
        title=title,
        page_url=f"https://suno.com/song/{clip_id}",
        audio_url=url,
        media_urls=[url],
        created_at=created_at,
    )


def _clip(n: int) -> str:
    return f"a1b2c3d4-1111-4111-8111-{n:012d}"


def _fetcher(handler) -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return f


def _seed(root: Path, name: str, *, data: bytes = b"x" * 4096, mtime: float) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


async def _run(tracks, fetcher, output_dir, **caps):
    return await download_tracks(tracks, fetcher, 1, output_dir, "{title}", "drinko", **caps)


# --- defaults: nothing changes without a cap --------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_without_caps_nothing_is_pruned_and_no_state_written(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    stats = await _run([_track(_clip(1), "One"), _track(_clip(2), "Two")], f, tmp_path)

    assert stats["downloaded"] == 2
    assert stats["rotated_tracks"] == 0
    assert stats["skipped_capped"] == 0
    assert not (tmp_path / STATE_FILENAME).exists()
    await f.__aexit__()


# --- rotate off: stop downloading at the cap, never delete ------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_count_cap_rotate_off_stops_downloading(tmp_path: Path) -> None:
    for i in range(4):
        _seed(tmp_path, f"Old {i}.m4a", mtime=1_000 + i)

    def handler(req):
        raise AssertionError("cap reached: must not download")

    f = _fetcher(handler)
    stats = await _run(
        [_track(_clip(1), "New A"), _track(_clip(2), "New B")],
        f,
        tmp_path,
        max_tracks=3,
    )
    assert stats["skipped_capped"] == 2
    assert stats["downloaded"] == 0
    assert stats["rotated_tracks"] == 0
    assert len(list(tmp_path.glob("*.m4a"))) == 4
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_count_cap_rotate_off_allows_the_remaining_room(tmp_path: Path) -> None:
    _seed(tmp_path, "Old.m4a", mtime=1_000)

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    stats = await _run(
        [_track(_clip(1), "New A"), _track(_clip(2), "New B"), _track(_clip(3), "New C")],
        f,
        tmp_path,
        max_tracks=3,
    )
    assert stats["downloaded"] == 2
    assert stats["skipped_capped"] == 1
    assert len(list(tmp_path.glob("*.m4a"))) == 3
    assert (tmp_path / "Old.m4a").exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_size_cap_rotate_off_stops_downloading(tmp_path: Path) -> None:
    _seed(tmp_path, "Old.m4a", data=b"x" * MEGABYTE, mtime=1_000)

    def handler(req):
        raise AssertionError("disk cap reached: must not download")

    f = _fetcher(handler)
    stats = await _run([_track(_clip(1), "New A")], f, tmp_path, max_disk_mb=1)
    assert stats["skipped_capped"] == 1
    assert stats["downloaded"] == 0
    assert (tmp_path / "Old.m4a").exists()
    await f.__aexit__()


# --- rotate on: delete the oldest until the archive fits --------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_count_cap_rotate_on_keeps_the_newest(tmp_path: Path) -> None:
    for i in range(3):
        _seed(tmp_path, f"Old {i}.m4a", mtime=1_000 + i)

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    stats = await _run([_track(_clip(1), "New")], f, tmp_path, max_tracks=2, rotate=True)

    assert stats["downloaded"] == 1
    assert stats["rotated_tracks"] == 2
    assert sorted(p.name for p in tmp_path.glob("*.m4a")) == ["New.m4a", "Old 2.m4a"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_size_cap_rotate_on_keeps_the_newest(tmp_path: Path) -> None:
    for i in range(3):
        _seed(tmp_path, f"Old {i}.m4a", data=b"x" * MEGABYTE, mtime=1_000 + i)

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    stats = await _run([_track(_clip(1), "New")], f, tmp_path, max_disk_mb=2, rotate=True)

    assert stats["rotated_tracks"] == 2  # two oldest of the four 1 MiB tracks
    assert sorted(p.name for p in tmp_path.glob("*.m4a")) == ["New.m4a", "Old 2.m4a"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_created_at_wins_over_mtime_when_pruning(tmp_path: Path) -> None:
    _seed(tmp_path, "Old.m4a", mtime=9_000)  # newest on disk
    _seed(tmp_path, "New.m4a", mtime=1_000)

    state = {
        "version": 1,
        "tracks": {
            "Old": {"id": _clip(9), "created_at": "2019-01-01T00:00:00Z"},
            "New": {"id": _clip(1), "created_at": "2025-01-01T00:00:00Z"},
        },
        "pruned": [],
    }
    (tmp_path / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    stats = await _run(
        [], _fetcher(lambda req: httpx.Response(200)), tmp_path, max_tracks=1, rotate=True
    )

    assert stats["rotated_tracks"] == 1
    assert not (tmp_path / "Old.m4a").exists()
    assert (tmp_path / "New.m4a").exists()


# --- sidecars go with their track -------------------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecars_are_deleted_with_their_track(tmp_path: Path) -> None:
    _seed(tmp_path, "Old.m4a", mtime=1_000)
    _seed(tmp_path, "Old.cover.jpg", mtime=1_000)
    _seed(tmp_path, "Old.mp4", mtime=1_000)
    _seed(tmp_path, "New.m4a", mtime=9_000)
    (tmp_path / "keep me.txt").write_text("not a track")

    stats = await _run(
        [], _fetcher(lambda req: httpx.Response(200)), tmp_path, max_tracks=1, rotate=True
    )

    assert (stats["rotated_tracks"], stats["rotated_files"], stats["rotated_bytes"]) == (
        1,
        3,
        3 * 4096,
    )
    assert not (tmp_path / "Old.m4a").exists()
    assert not (tmp_path / "Old.cover.jpg").exists()
    assert not (tmp_path / "Old.mp4").exists()
    assert (tmp_path / "New.m4a").exists()
    assert (tmp_path / "keep me.txt").exists()


# --- a pruned track is not re-downloaded ------------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_pruned_tracks_are_not_redownloaded(tmp_path: Path) -> None:
    tracks = [_track(_clip(i), f"Song {i}") for i in range(3)]
    fetched: list[str] = []

    def handler(req):
        fetched.append(str(req.url))
        return httpx.Response(200, content=_VALID_MEDIA)

    # First run: the newest track survives, the two older ones are pruned.
    first = await _run(tracks, _fetcher(handler), tmp_path, max_tracks=1, rotate=True)
    assert first["downloaded"] == 3
    assert first["rotated_tracks"] == 2
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["Song 2.m4a"]

    # Second run, same discovery: the pruned tracks must not be fetched again.
    fetched.clear()
    second = await _run(tracks, _fetcher(handler), tmp_path, max_tracks=1, rotate=True)
    assert second["skipped_rotated"] == 2
    assert second["skipped_exists"] == 1
    assert second["downloaded"] == 0
    assert fetched == []  # no network at all: nothing missing, nothing pruned-back
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["Song 2.m4a"]
    assert json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))["pruned"] == [
        _clip(0),
        _clip(1),
    ]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_created_at_recorded_for_the_next_rotation(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    await _run(
        [_track(_clip(1), "One", created_at="2025-02-03T04:05:06Z")],
        f,
        tmp_path,
        max_tracks=5,
        rotate=True,
    )
    state = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["tracks"]["One"] == {"id": _clip(1), "created_at": "2025-02-03T04:05:06Z"}
    await f.__aexit__()


# --- guards ----------------------------------------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_rotation_refuses_to_delete_everything(tmp_path: Path, caplog) -> None:
    only = _seed(tmp_path, "Only.m4a", data=b"x" * (2 * MEGABYTE), mtime=1)

    with caplog.at_level("ERROR"):
        stats = await _run(
            [], _fetcher(lambda req: httpx.Response(200)), tmp_path, max_disk_mb=1, rotate=True
        )

    assert stats["rotated_tracks"] == 0
    assert only.exists()
    assert any("Rotation refused" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_rotate_dry_run_reports_without_deleting(tmp_path: Path, caplog) -> None:
    old = _seed(tmp_path, "Old.m4a", mtime=1)
    _seed(tmp_path, "New.m4a", mtime=9)

    def handler(req):
        raise AssertionError("dry run must not fetch")

    with caplog.at_level("INFO"):
        stats = await _run(
            [_track(_clip(1), "Incoming")],
            _fetcher(handler),
            tmp_path,
            max_tracks=1,
            rotate=True,
            dry_run=True,
        )

    assert stats["dry_run"] == 1
    assert stats["rotated_tracks"] == 1
    assert old.exists()
    assert not (tmp_path / "Incoming.m4a").exists()
    assert any("[dry-run] Rotation would remove" in r.getMessage() for r in caplog.records)
