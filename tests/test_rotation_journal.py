"""Crash-consistent rotation accounting (#147).

The journal is exercised at every point a process could die: after planning,
between unlinks, after the last unlink but before the state save, and after
completion. Recovery only reconciles — it must never delete media.
"""

from __future__ import annotations

import json
from pathlib import Path

from suno_dl.rotate import (
    JOURNAL_FILENAME,
    RotationPlan,
    RotationState,
    TrackGroup,
    apply_rotation,
    begin_journal,
    plan_rotation,
    recover_rotation,
    rotate_archive,
)

CLIP_A = "a1b2c3d4-1111-4111-8111-111111111111"
CLIP_B = "b2c3d4e5-2222-4222-8222-222222222222"
MEDIA = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096


def _track(root: Path, stem: str, clip_id: str, *, cover: bool = True) -> Path:
    folder = root / "drinko"
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{stem}.m4a"
    audio.write_bytes(MEDIA)
    files = [audio.name]
    if cover:
        (folder / f"{stem}.cover.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)
        files.append(f"{stem}.cover.jpg")
    (folder / f"{stem}.json").write_text(
        json.dumps({"schema": 1, "id": clip_id, "title": stem, "saved_files": files}),
        encoding="utf-8",
    )
    return audio


def _plan(root: Path, stems: list[str]) -> RotationPlan:
    groups = []
    for stem in stems:
        folder = root / "drinko"
        files = tuple(sorted(folder.glob(f"{stem}.*")))
        groups.append(
            TrackGroup(
                key=f"drinko/{stem}",
                files=files,
                size=sum(path.stat().st_size for path in files),
                created_at="2026-01-01T00:00:00Z",
                clip_id=CLIP_A,
            )
        )
    return RotationPlan(
        tuple(groups), tracks_before=len(stems), bytes_before=0, tracks_after=0, bytes_after=0
    )


def test_crash_between_unlinks_keeps_partial_group_suppressible(tmp_path: Path) -> None:
    _track(tmp_path, "Song", CLIP_A)
    plan = _plan(tmp_path, ["Song"])
    begin_journal(tmp_path, plan)
    # Simulate a crash after the cover went but before the audio did.
    (tmp_path / "drinko" / "Song.cover.jpg").unlink()
    state = RotationState()
    state.record(tmp_path, tmp_path / "drinko" / "Song", clip_id=CLIP_A, created_at=None)

    stats = recover_rotation(tmp_path, state)
    assert stats["partial_groups"] == 1
    assert (tmp_path / "drinko" / "Song.m4a").exists(), "recovery never deletes"
    assert state.tracks, "a group with media left keeps its state entry"
    assert state.pruned == [], "a half-present group must not be suppressed"


def test_crash_after_last_unlink_marks_the_group_pruned(tmp_path: Path) -> None:
    _track(tmp_path, "Song", CLIP_A)
    plan = _plan(tmp_path, ["Song"])
    begin_journal(tmp_path, plan)
    for path in (tmp_path / "drinko").glob("Song.*"):
        path.unlink()
    state = RotationState()
    state.record(tmp_path, tmp_path / "drinko" / "Song", clip_id=CLIP_A, created_at=None)

    stats = recover_rotation(tmp_path, state)
    assert stats["recovered_groups"] == 1
    assert state.pruned == [CLIP_A]
    assert not state.tracks
    assert state.pruned == RotationState.load(tmp_path).pruned, "recovery persists state"
    assert not (tmp_path / JOURNAL_FILENAME).exists()


def test_recovery_is_idempotent_and_never_deletes(tmp_path: Path) -> None:
    _track(tmp_path, "Song", CLIP_A)
    plan = _plan(tmp_path, ["Song"])
    begin_journal(tmp_path, plan)
    stray = tmp_path / "drinko" / "Unrelated.m4a"
    stray.write_bytes(MEDIA)

    first = recover_rotation(tmp_path)
    second = recover_rotation(tmp_path)
    assert first == {"recovered_groups": 0, "partial_groups": 1}
    # The journal is retired after one reconciliation; a second pass is a no-op.
    assert second == {"recovered_groups": 0, "partial_groups": 0}
    assert stray.read_bytes() == MEDIA


def test_corrupt_journal_is_ignored(tmp_path: Path) -> None:
    (tmp_path / JOURNAL_FILENAME).write_text("{not json\n", encoding="utf-8")
    state = RotationState()
    assert recover_rotation(tmp_path, state) == {"recovered_groups": 0, "partial_groups": 0}
    assert not (tmp_path / JOURNAL_FILENAME).exists()


def test_successful_rotation_retires_the_journal(tmp_path: Path) -> None:
    for stem, clip in (("Old", CLIP_A), ("New", CLIP_B)):
        _track(tmp_path, stem, clip)
    state = RotationState()
    state.record(
        tmp_path, tmp_path / "drinko" / "Old", clip_id=CLIP_A, created_at="2025-01-01T00:00:00Z"
    )
    state.record(
        tmp_path, tmp_path / "drinko" / "New", clip_id=CLIP_B, created_at="2026-01-01T00:00:00Z"
    )
    stats = rotate_archive(tmp_path, max_tracks=1, state=state)
    assert stats["rotated_tracks"] == 1
    assert not (tmp_path / JOURNAL_FILENAME).exists()
    assert RotationState.load(tmp_path).pruned == [CLIP_A]


def test_planning_does_not_touch_the_journal(tmp_path: Path) -> None:
    _track(tmp_path, "Old", CLIP_A)
    _track(tmp_path, "New", CLIP_B)
    plan = plan_rotation(tmp_path, max_tracks=1)
    assert plan.remove
    assert not (tmp_path / JOURNAL_FILENAME).exists()


def test_dry_run_writes_no_journal(tmp_path: Path) -> None:
    _track(tmp_path, "Old", CLIP_A)
    _track(tmp_path, "New", CLIP_B)
    state = RotationState()
    stats = rotate_archive(tmp_path, max_tracks=1, dry_run=True, state=state)
    assert stats["rotated_tracks"] == 1
    assert not (tmp_path / JOURNAL_FILENAME).exists()
    assert RotationState.load(tmp_path).pruned == []


def test_journal_header_records_target_files_before_unlink(tmp_path: Path) -> None:
    _track(tmp_path, "Song", CLIP_A)
    plan = _plan(tmp_path, ["Song"])
    path = begin_journal(tmp_path, plan)
    assert path is not None
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["phase"] == "planned"
    assert header["groups"][0]["key"] == "drinko/Song"
    assert set(header["groups"][0]["files"]) == {"Song.m4a", "Song.cover.jpg", "Song.json"}


def test_apply_records_every_removal_then_completion(tmp_path: Path) -> None:
    _track(tmp_path, "Song", CLIP_A)
    plan = _plan(tmp_path, ["Song"])
    stats = apply_rotation(tmp_path, plan, state=None)
    assert stats["rotated_files"] == len(plan.remove[0].files)
    lines = (tmp_path / JOURNAL_FILENAME).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert events[-1]["event"] == "completed"
    assert sum(event.get("event") == "removed" for event in events) == stats["rotated_files"]
