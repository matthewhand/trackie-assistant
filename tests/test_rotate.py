"""Tests for the rotation module: grouping, ordering, caps and guards (#93)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from suno_dl.rotate import (
    MEGABYTE,
    STATE_FILENAME,
    RotationPlan,
    RotationState,
    TrackGroup,
    apply_rotation,
    archive_usage,
    plan_rotation,
    rotate_archive,
    scan_groups,
)


def _touch(path: Path, data: bytes = b"x" * 4096, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """Create a symlink, or skip: Windows needs a privilege for them."""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks not permitted here: {exc}")


# --- grouping: what counts as one track -------------------------------------


def test_group_collects_media_variants_and_covers(tmp_path: Path) -> None:
    _touch(tmp_path / "drinko_Song.m4a")
    _touch(tmp_path / "drinko_Song.mp4")
    _touch(tmp_path / "drinko_Song.cover.jpg")
    _touch(tmp_path / "Other Song.m4a")

    groups = {g.key: g for g in scan_groups(tmp_path)}
    assert set(groups) == {"drinko_Song", "Other Song"}
    assert sorted(p.name for p in groups["drinko_Song"].files) == [
        "drinko_Song.cover.jpg",
        "drinko_Song.m4a",
        "drinko_Song.mp4",
    ]
    assert groups["drinko_Song"].size == 3 * 4096


def test_ignores_temporaries_dotfiles_and_unknown_files(tmp_path: Path) -> None:
    _touch(tmp_path / "Song.m4a")
    _touch(tmp_path / "Song.m4a.part")
    _touch(tmp_path / ".suno-rotation.json")
    _touch(tmp_path / ".cover.jpg")  # a cover with no track is not a track
    (tmp_path / "notes.txt").write_text("hello")

    groups = scan_groups(tmp_path)
    assert [g.key for g in groups] == ["Song"]
    assert [p.name for p in groups[0].files] == ["Song.m4a"]


def test_unparseable_created_at_falls_back_to_mtime(tmp_path: Path) -> None:
    _touch(tmp_path / "Broken.m4a", mtime=1)
    _touch(tmp_path / "Fine.m4a", mtime=9)
    state = _state_with(
        Broken={"id": "x", "created_at": "not-a-timestamp"},
        Fine={"id": "y", "created_at": "2025-01-01T00:00:00Z"},
    )

    plan = plan_rotation(tmp_path, max_tracks=1, state=state)
    assert [g.key for g in plan.remove] == ["Broken"]


def test_no_caps_is_always_a_noop(tmp_path: Path) -> None:
    _touch(tmp_path / "Song.m4a")
    stats = rotate_archive(tmp_path)
    assert stats == {"rotated_tracks": 0, "rotated_files": 0, "rotated_bytes": 0}
    assert (tmp_path / "Song.m4a").exists()


def test_groups_nest_per_source_subfolder(tmp_path: Path) -> None:
    _touch(tmp_path / "alpha" / "Song.m4a")
    _touch(tmp_path / "beta" / "Song.m4a")

    assert sorted(g.key for g in scan_groups(tmp_path)) == ["alpha/Song", "beta/Song"]


def test_symlinks_are_neither_followed_nor_managed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "archive"
    outside = tmp_path / "outside"
    _touch(outside / "Secret.m4a")
    _touch(archive / "alpha" / "Song.m4a")
    # A symlinked directory that escapes the archive must not be traversed,
    # and a symlinked file must not be counted or sized.
    _symlink(archive / "link", outside, directory=True)
    _symlink(archive / "alpha" / "Linked.m4a", outside / "Secret.m4a")

    with caplog.at_level("DEBUG"):
        groups = scan_groups(archive)
    assert [g.key for g in groups] == ["alpha/Song"]
    assert groups[0].size == 4096


# --- ordering: created_at first, mtime as the fallback ----------------------


def _state_with(**entries: dict[str, str]) -> RotationState:
    return RotationState(tracks={key: dict(value) for key, value in entries.items()})


def test_created_at_from_state_orders_pruning(tmp_path: Path) -> None:
    _touch(tmp_path / "New.m4a", mtime=1_000_000)  # newest by mtime
    _touch(tmp_path / "Old.m4a", mtime=9_000_000)  # newest mtime, oldest created
    state = _state_with(
        New={"id": "new-id", "created_at": "2025-06-01T00:00:00Z"},
        Old={"id": "old-id", "created_at": "2020-01-01T00:00:00Z"},
    )

    plan = plan_rotation(tmp_path, max_tracks=1, state=state)
    assert [g.key for g in plan.remove] == ["Old"]
    assert plan.tracks_before == 2 and plan.tracks_after == 1


def test_mtime_is_the_fallback_without_state(tmp_path: Path) -> None:
    _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=9_000)

    plan = plan_rotation(tmp_path, max_tracks=1)
    assert [g.key for g in plan.remove] == ["Old"]


# --- caps: count and size ----------------------------------------------------


def test_count_cap_prunes_oldest_until_under(tmp_path: Path) -> None:
    for i in range(4):
        _touch(tmp_path / f"S{i}.m4a", mtime=1_000 + i)

    plan = plan_rotation(tmp_path, max_tracks=2)
    assert [g.key for g in plan.remove] == ["S0", "S1"]
    assert plan.tracks_after == 2


def test_size_cap_prunes_oldest_until_under(tmp_path: Path) -> None:
    for i in range(3):
        _touch(tmp_path / f"S{i}.m4a", data=b"x" * MEGABYTE, mtime=1_000 + i)

    plan = plan_rotation(tmp_path, max_disk_mb=2)
    assert [g.key for g in plan.remove] == ["S0"]
    assert plan.bytes_after == 2 * MEGABYTE


def test_both_caps_together(tmp_path: Path) -> None:
    for i in range(3):
        _touch(tmp_path / f"S{i}.m4a", data=b"x" * MEGABYTE, mtime=1_000 + i)

    plan = plan_rotation(tmp_path, max_tracks=3, max_disk_mb=2)
    assert [g.key for g in plan.remove] == ["S0"]


def test_no_caps_means_no_pruning(tmp_path: Path) -> None:
    _touch(tmp_path / "S0.m4a")
    plan = plan_rotation(tmp_path)
    assert plan.remove == ()
    assert plan.refused is False


# --- guards ------------------------------------------------------------------


def test_refuses_a_plan_that_would_delete_everything(tmp_path: Path) -> None:
    _touch(tmp_path / "Only.m4a", data=b"x" * (2 * MEGABYTE))

    plan = plan_rotation(tmp_path, max_disk_mb=1)
    assert plan.refused is True
    assert [g.key for g in plan.remove] == ["Only"]
    assert "empty the whole archive" in plan.reason

    stats = rotate_archive(tmp_path, max_disk_mb=1)
    assert stats == {"rotated_tracks": 0, "rotated_files": 0, "rotated_bytes": 0}
    assert (tmp_path / "Only.m4a").exists()


def test_partial_delete_is_allowed_even_at_a_tiny_cap(tmp_path: Path) -> None:
    # One of three tracks is spared, so the plan is not "delete everything".
    _touch(tmp_path / "A.m4a", data=b"x" * MEGABYTE, mtime=1)
    _touch(tmp_path / "B.m4a", data=b"x" * MEGABYTE, mtime=2)
    _touch(tmp_path / "C.m4a", data=b"x" * MEGABYTE, mtime=3)

    stats = rotate_archive(tmp_path, max_disk_mb=2)
    assert stats["rotated_tracks"] == 1
    assert not (tmp_path / "A.m4a").exists()
    assert (tmp_path / "C.m4a").exists()


def test_apply_refuses_paths_outside_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _touch(archive / "Song.m4a")
    outside = _touch(tmp_path / "outside" / "Keep.m4a")

    plan = RotationPlan(
        remove=(
            TrackGroup(
                key="Song",
                files=(archive / "Song.m4a",),
                size=4096,
                created_at=None,
                clip_id=None,
            ),
            TrackGroup(
                key="../outside/Keep",
                files=(outside,),
                size=4096,
                created_at=None,
                clip_id=None,
            ),
        ),
        tracks_before=2,
        bytes_before=8192,
        tracks_after=0,
        bytes_after=0,
    )
    stats = apply_rotation(archive, plan)

    assert outside.exists()
    assert not (archive / "Song.m4a").exists()
    assert stats["rotated_files"] == 1


def test_apply_refuses_symlinks_even_inside_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    real = _touch(tmp_path / "outside" / "Secret.m4a")
    link = archive / "Linked.m4a"
    link.parent.mkdir(parents=True, exist_ok=True)
    _symlink(link, real)

    plan = replace(
        plan_rotation(archive, max_tracks=1),
        remove=(TrackGroup(key="Linked", files=(link,), size=0, created_at=None, clip_id=None),),
    )
    stats = apply_rotation(archive, plan)

    assert link.is_symlink() and real.exists()
    assert stats == {"rotated_tracks": 1, "rotated_files": 0, "rotated_bytes": 0}


# --- dry run -----------------------------------------------------------------


def test_dry_run_reports_the_plan_and_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _touch(tmp_path / "Old.m4a", mtime=1)
    _touch(tmp_path / "New.m4a", mtime=9)

    with caplog.at_level("INFO"):
        stats = rotate_archive(tmp_path, max_tracks=1, dry_run=True)

    assert stats["rotated_tracks"] == 1
    assert stats["rotated_files"] == 1
    assert (tmp_path / "Old.m4a").exists()
    assert any("[dry-run]" in record.message for record in caplog.records)


def test_every_removal_is_logged_individually(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    old_media = _touch(tmp_path / "Old.m4a", mtime=1)
    old_cover = _touch(tmp_path / "Old.cover.jpg", mtime=1)
    _touch(tmp_path / "New.m4a", mtime=9)

    with caplog.at_level("INFO"):
        stats = rotate_archive(tmp_path, max_tracks=1)

    assert stats == {"rotated_tracks": 1, "rotated_files": 2, "rotated_bytes": 8192}
    removals = [r.getMessage() for r in caplog.records if "Rotation removed" in r.getMessage()]
    assert sorted(removals) == sorted(
        [
            f"Rotation removed {old_media} (4096 bytes)",
            f"Rotation removed {old_cover} (4096 bytes)",
        ]
    )


# --- state: ages survive between runs, pruned ids do not come back -----------


def test_state_roundtrip_and_pruned_ids(tmp_path: Path) -> None:
    state = RotationState()
    state.record(tmp_path, tmp_path / "drinko" / "Song", clip_id="clip-1", created_at=None)
    state.record(
        tmp_path, tmp_path / "drinko" / "Other", clip_id="clip-2", created_at="2025-01-02T00:00:00Z"
    )
    state.mark_pruned("clip-2")
    state.forget("drinko/Other")
    state.save(tmp_path)

    assert (tmp_path / STATE_FILENAME).exists()
    loaded = RotationState.load(tmp_path)
    assert set(loaded.tracks) == {"drinko/Song"}
    assert loaded.pruned == ["clip-2"]


def test_missing_state_is_empty(tmp_path: Path) -> None:
    assert RotationState.load(tmp_path).tracks == {}


def test_corrupt_state_is_ignored(tmp_path: Path) -> None:
    (tmp_path / STATE_FILENAME).write_text("{not json", encoding="utf-8")
    loaded = RotationState.load(tmp_path)
    assert loaded.tracks == {} and loaded.pruned == []


def test_pruned_ids_are_recorded_on_disk_when_a_group_is_pruned(tmp_path: Path) -> None:
    _touch(tmp_path / "Old.m4a", mtime=1)
    _touch(tmp_path / "New.m4a", mtime=9)
    state = _state_with(
        Old={"id": "old-clip", "created_at": "2020-01-01T00:00:00Z"},
        New={"id": "new-clip", "created_at": "2025-01-01T00:00:00Z"},
    )

    rotate_archive(tmp_path, max_tracks=1, state=state)

    on_disk = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["pruned"] == ["old-clip"]
    assert "Old" not in on_disk["tracks"]


def test_archive_usage_counts_tracks_and_bytes(tmp_path: Path) -> None:
    _touch(tmp_path / "a" / "S1.m4a")
    _touch(tmp_path / "a" / "S1.cover.jpg")
    _touch(tmp_path / "b" / "S2.m4a", data=b"x" * 2048)

    assert archive_usage(tmp_path) == (2, 4096 + 4096 + 2048)


def test_archive_usage_of_a_missing_dir(tmp_path: Path) -> None:
    assert archive_usage(tmp_path / "nope") == (0, 0)


# --- id sidecars: pruned with their track, never anything else ---------------

_UUID_A = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
_UUID_B = "a1b2c3d4-1111-4111-8111-bbbbbbbbbbbb"


def _id_sidecar(media: Path, clip_id: str, *, saved: list[str] | None = None) -> Path:
    """Write a genuine suno-dl id sidecar next to ``media`` (``<stem>.json``)."""
    from suno_dl.models import TrackInfo
    from suno_dl.sidecar import build_sidecar, sidecar_path, write_sidecar

    track = TrackInfo(id=clip_id, title="T", page_url=f"https://suno.com/song/{clip_id}")
    path = sidecar_path(media)
    write_sidecar(
        path, build_sidecar(track, handle="h", saved_files=saved or [media.name], source="test")
    )
    return path


def test_sidecar_joins_its_tracks_group(tmp_path: Path) -> None:
    media = _touch(tmp_path / "Song.m4a")
    _touch(tmp_path / "Song.cover.jpg")
    side = _id_sidecar(media, _UUID_A)

    (group,) = scan_groups(tmp_path)
    assert side in group.files
    assert sorted(p.name for p in group.files) == ["Song.cover.jpg", "Song.json", "Song.m4a"]
    assert group.size == 2 * 4096 + side.stat().st_size
    assert group.clip_id == _UUID_A  # no rotation state needed to remember the id


def test_rotation_deletes_a_pruned_tracks_id_sidecar_and_only_that_one(tmp_path: Path) -> None:
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    new = _touch(tmp_path / "New.m4a", mtime=2_000)
    old_side = _id_sidecar(old, _UUID_A)
    new_side = _id_sidecar(new, _UUID_B)
    state = RotationState()

    stats = rotate_archive(tmp_path, max_tracks=1, state=state)

    assert stats["rotated_tracks"] == 1 and stats["rotated_files"] == 2
    assert not old.exists() and not old_side.exists()
    assert new.exists() and new_side.exists()  # the unpruned track keeps its sidecar
    assert _UUID_A in state.pruned and _UUID_B not in state.pruned


def test_a_sidecar_mtime_does_not_change_a_tracks_age(tmp_path: Path) -> None:
    # A backfilled sidecar is written *now*; the track is old and must still go first.
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    new = _touch(tmp_path / "New.m4a", mtime=2_000)
    _id_sidecar(old, _UUID_A)  # mtime = now, newer than both tracks
    plan = plan_rotation(tmp_path, max_tracks=1)
    assert [g.key for g in plan.remove] == ["Old"]
    assert new.exists()


def test_a_foreign_json_beside_a_track_is_not_deleted(tmp_path: Path) -> None:
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=2_000)
    mine = tmp_path / "Old.json"
    mine.write_text(json.dumps({"title": "someone else's metadata"}))
    other = tmp_path / "Old.notes.json"
    other.write_text("{}")
    broken = tmp_path / "Old.json.bak"
    broken.write_text("{")

    rotate_archive(tmp_path, max_tracks=1)

    assert not old.exists()
    assert mine.exists() and other.exists() and broken.exists()


def test_a_sidecar_that_does_not_list_this_track_is_not_owned(tmp_path: Path) -> None:
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=2_000)
    # Valid sidecar, but it describes some other file: not this track's to delete.
    stray = _id_sidecar(old, _UUID_A, saved=["Elsewhere.m4a"])

    rotate_archive(tmp_path, max_tracks=1)

    assert not old.exists() and stray.exists()


def test_a_sidecar_whose_id_disagrees_with_the_state_is_not_deleted(tmp_path: Path) -> None:
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=2_000)
    side = _id_sidecar(old, _UUID_B)
    state = _state_with(Old={"id": _UUID_A})

    rotate_archive(tmp_path, max_tracks=1, state=state)

    assert not old.exists() and side.exists()
    assert state.pruned == [_UUID_A]


def test_an_orphan_sidecar_without_a_track_is_left_alone(tmp_path: Path) -> None:
    _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=2_000)
    orphan = _id_sidecar(tmp_path / "Gone.m4a", _UUID_A)
    assert orphan.exists()

    groups = scan_groups(tmp_path)
    assert sorted(g.key for g in groups) == ["New", "Old"]
    rotate_archive(tmp_path, max_tracks=1)
    assert orphan.exists()


def test_sidecars_in_subfolders_follow_their_own_track(tmp_path: Path) -> None:
    old = _touch(tmp_path / "alpha" / "Song.m4a", mtime=1_000)
    keep = _touch(tmp_path / "beta" / "Song.m4a", mtime=2_000)
    old_side = _id_sidecar(old, _UUID_A)
    keep_side = _id_sidecar(keep, _UUID_B)

    rotate_archive(tmp_path, max_tracks=1)

    assert not old.exists() and not old_side.exists()
    assert keep.exists() and keep_side.exists()


def test_a_symlinked_sidecar_is_never_followed_or_deleted(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    outside = tmp_path / "outside"
    old = _touch(archive / "Old.m4a", mtime=1_000)
    _touch(archive / "New.m4a", mtime=2_000)
    outside.mkdir()
    target = _id_sidecar(outside / "Real.m4a", _UUID_A, saved=["Old.m4a"])
    link = archive / "Old.json"
    _symlink(link, target)

    groups = {g.key: g for g in scan_groups(archive)}
    assert link not in groups["Old"].files

    rotate_archive(archive, max_tracks=1)

    assert not old.exists()
    assert link.is_symlink() and target.exists()  # neither the link nor its target went


def test_apply_refuses_a_sidecar_that_became_a_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    outside = tmp_path / "outside"
    old = _touch(archive / "Old.m4a", mtime=1_000)
    _touch(archive / "New.m4a", mtime=2_000)
    side = _id_sidecar(old, _UUID_A)
    plan = plan_rotation(archive, max_tracks=1)
    assert side in plan.remove[0].files

    # Between planning and applying the sidecar is swapped for a link out of the archive.
    secret = _touch(outside / "secret.json", b"{}")
    side.unlink()
    _symlink(side, secret)

    apply_rotation(archive, plan)

    assert not old.exists()
    assert side.is_symlink() and secret.exists()


def test_dry_run_leaves_the_sidecar_too(tmp_path: Path) -> None:
    old = _touch(tmp_path / "Old.m4a", mtime=1_000)
    _touch(tmp_path / "New.m4a", mtime=2_000)
    side = _id_sidecar(old, _UUID_A)
    stats = rotate_archive(tmp_path, max_tracks=1, dry_run=True, state=RotationState())
    assert stats["rotated_files"] == 2
    assert old.exists() and side.exists()
