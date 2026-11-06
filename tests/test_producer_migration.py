import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from suno_dl import producer_migration as migration

_REAL_ARTIST_MATCHES = migration._artist_matches

_REAL_PLAN = migration.plan_migration


def _apply_approved(root, producer_by_id, **kwargs):
    # Disposable tests model an operator review; production callers must
    # independently inspect the complete plan and producer evidence.
    digest = (
        migration.review_plan_digest(
            _REAL_PLAN(
                root, producer_by_id, creator_handle_by_id=kwargs.get("creator_handle_by_id")
            )
        )
        if sys.platform.startswith("linux")
        else "0" * 64
    )
    return migration.apply_migration(
        root,
        producer_by_id,
        producer_mapping_reviewed=True,
        reviewed_producers=dict(producer_by_id),
        reviewed_plan_sha256=digest,
        **kwargs,
    )


def _resume_approved(root, **kwargs):
    journal = migration.journal_path(root)
    return migration.resume_migration(
        root,
        producer_mapping_reviewed=True,
        reviewed_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def dummy_media_has_no_tag_work(monkeypatch):
    # Most filename tests use deliberately undecodable byte fixtures.
    monkeypatch.setattr(migration, "_artist_matches", lambda _path, _artist: True)


ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"


def recording(root: Path, stem: str, clip_id: str = ID1, title: str = "A song") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    media = root / (stem + ".m4a")
    media.write_bytes(b"a" * 2048)
    cover = root / (stem + ".cover.jpeg")
    cover.write_bytes(b"cover")
    (root / (stem + ".json")).write_text(
        json.dumps(
            {
                "id": clip_id,
                "title": title,
                "artist": "untrusted legacy",
                "saved_files": [media.name, cover.name],
            }
        )
    )
    return media


@pytest.mark.parametrize(
    "extra",
    [
        '"id":"11111111-1111-4111-8111-111111111111"',
        '"id":"22222222-2222-4222-8222-222222222222"',
        '"saved_files":["drinko_A song.m4a"]',
        '"saved_files":["different.m4a"]',
        '"extra":{"nested":1,"nested":1}',
        '"extra":{"nested":1,"nested":2}',
        '"extra":{"nested":NaN}',
        '"extra":{"nested":Infinity}',
        '"extra":{"nested":-Infinity}',
        '"extra":{"nested":1e999}',
        '"extra":{"nested":-1e999}',
    ],
)
def test_plan_rejects_duplicate_fields_at_any_depth(tmp_path, extra):
    recording(tmp_path, "drinko_A song")
    sidecar = tmp_path / "drinko_A song.json"
    original = sidecar.read_text()
    sidecar.write_text(original[:-1] + "," + extra + "}")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Unrecognized sidecar"):
        migration.plan_migration(tmp_path, {ID1: "Producer", ID2: "Other"})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("extension", [".aac", ".flac", ".m4b", ".ogg", ".FLAC"])
@pytest.mark.parametrize("mixed", [False, True])
def test_plan_refuses_unsupported_audio_even_in_otherwise_valid_archive(tmp_path, extension, mixed):
    if mixed:
        recording(tmp_path, "drinko_A song")
    unsupported = tmp_path / ("unowned" + extension)
    unsupported.write_bytes(b"x" * 2048)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Unowned media or unsupported audio"):
        migration.plan_migration(tmp_path, {ID1: "Producer"} if mixed else {})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("extension", [".m4a", ".mp3", ".mp4", ".wav", ".flac", ".OGG"])
def test_reserved_metadata_tree_audio_blocks_empty_plan(tmp_path, extension):
    hidden = tmp_path / ".library" / "nested"
    hidden.mkdir(parents=True)
    audio = hidden / ("stray" + extension)
    audio.write_bytes(b"x" * 2048)
    with pytest.raises(migration.MigrationError, match="Audio in reserved metadata"):
        migration.plan_migration(tmp_path, {})
    assert audio.read_bytes() == b"x" * 2048


@pytest.mark.parametrize("extension", [".cue", ".xspf", ".M3U"])
def test_reserved_metadata_tree_known_reference_blocks_rename(tmp_path, extension):
    recording(tmp_path, "drinko_A song")
    metadata = tmp_path / ".library" / "nested"
    metadata.mkdir(parents=True)
    reference = metadata / ("playlist" + extension)
    reference.write_text('FILE "../../drinko_A song.m4a" WAVE\n')
    with pytest.raises(migration.MigrationError, match="Filename-referencing state"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert reference.read_text().startswith("FILE")


def test_reserved_metadata_tree_symlink_is_not_followed(tmp_path):
    metadata = tmp_path / ".library"
    metadata.mkdir()
    (metadata / "elsewhere").symlink_to(tmp_path)
    with pytest.raises(migration.MigrationError, match="Symlink in reserved metadata"):
        migration.plan_migration(tmp_path, {})


def test_normal_reserved_metadata_does_not_block_plan(tmp_path):
    recording(tmp_path, "drinko_A song")
    nested = tmp_path / ".library" / "nested"
    nested.mkdir(parents=True)
    (nested / "state.json").write_text("{}")
    plan = migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert len(plan["groups"]) == 1
    assert (nested / "state.json").read_text() == "{}"


def test_plan_accepts_normal_legacy_sidecar(tmp_path):
    recording(tmp_path, "drinko_A song")
    plan = migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert plan["groups"][0]["id"] == ID1


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_read_only_plan_uses_explicit_producer_and_moves_group(tmp_path):
    old = recording(tmp_path, "drinko_A song")
    before = sorted(p.name for p in tmp_path.iterdir())
    plan = migration.plan_migration(tmp_path, {ID1: "Actual Producer"})
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert plan["groups"][0]["new_sidecar"] == "Actual Producer_A song.json"
    assert _apply_approved(tmp_path, {ID1: "Actual Producer"}, writers_stopped=True) == 1
    new = tmp_path / "Actual Producer_A song.m4a"
    assert not old.exists() and new.read_bytes() == b"a" * 2048
    assert (tmp_path / "Actual Producer_A song.cover.jpeg").read_bytes() == b"cover"
    data = json.loads((tmp_path / "Actual Producer_A song.json").read_text())
    assert data["saved_files"] == [new.name, "Actual Producer_A song.cover.jpeg"]
    assert data["artist"] == "Actual Producer"  # explicit verified producer
    assert not migration.journal_path(tmp_path).exists()
    assert _apply_approved(tmp_path, {ID1: "Actual Producer"}, writers_stopped=True) == 0


def test_unknown_must_be_explicit(tmp_path):
    recording(tmp_path, "drinko_A song")
    with pytest.raises(migration.MigrationError, match="No verified producer"):
        migration.plan_migration(tmp_path, {})
    plan = migration.plan_migration(tmp_path, {ID1: None})
    assert plan["groups"][0]["new_sidecar"] == "Unknown artist_A song.json"
    with pytest.raises(migration.MigrationError, match="stopped"):
        _apply_approved(tmp_path, {ID1: None})


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_collision_uses_uuid_and_never_overwrites(tmp_path):
    recording(tmp_path, "one", ID1)
    recording(tmp_path, "two", ID2)
    existing = tmp_path / "Producer_A song.m4a"
    existing.write_bytes(b"user file")
    with pytest.raises(migration.MigrationError, match="Unowned media"):
        migration.plan_migration(tmp_path, {ID1: "Producer", ID2: "Producer"})
    existing.unlink()
    _apply_approved(tmp_path, {ID1: "Producer", ID2: "Producer"}, writers_stopped=True)
    assert (tmp_path / "Producer_A song.m4a").exists()
    assert (tmp_path / f"Producer_A song_{ID2}.m4a").exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_interruption_resumes_without_duplicates(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace
    count = 0

    def interrupted(old, new):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("power loss")
        return real(old, new)

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="power loss"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    with pytest.raises(migration.MigrationError, match="journal"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    assert _resume_approved(tmp_path, writers_stopped=True) == 1
    assert len(list(tmp_path.glob("*.m4a"))) == 1
    assert (tmp_path / "Producer_A song.m4a").read_bytes() == b"a" * 2048


@pytest.mark.parametrize("blocker", ["playlist", "rotation", "symlink", "unowned", "duplicate"])
def test_unsafe_library_is_refused(tmp_path, blocker):
    recording(tmp_path, "drinko_A song")
    if blocker == "playlist":
        (tmp_path / "mix.m3u").write_text("drinko_A song.m4a")
    elif blocker == "rotation":
        (tmp_path / ".suno-rotation.json").write_text("{}")
    elif blocker == "symlink":
        if sys.platform == "win32":
            pytest.skip("creating symlinks may require Windows privileges")
        (tmp_path / "linked").symlink_to(tmp_path / "drinko_A song.m4a")
    elif blocker == "unowned":
        (tmp_path / "other.m4a").write_bytes(b"x" * 2048)
    else:
        recording(tmp_path, "second")
    with pytest.raises(migration.MigrationError):
        migration.plan_migration(tmp_path, {ID1: "Producer"})


@pytest.mark.parametrize("extension", [".cue", ".xspf", ".wpl", ".asx", ".wax", ".wvx", ".CUE"])
def test_known_file_reference_formats_block_renames(tmp_path, extension):
    media = recording(tmp_path, "drinko_A song")
    reference = tmp_path / ("playlist" + extension)
    reference.write_text('FILE "drinko_A song.m4a" WAVE\n')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Filename-referencing state"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert media.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize(
    "added",
    [
        "new.cue",
        "nested/list.XSPF",
        "new.flac",
        "new.mp3",
        "new.json",
        "new.cover.jpg",
        "new.lrc",
        ".library/nested/new.OGG",
        ".library/nested/list.cue",
    ],
)
def test_unfinished_resume_refuses_new_unreviewed_members_before_moves(
    tmp_path, monkeypatch, added
):
    recording(tmp_path, "drinko_A song")
    rename = migration._rename_no_replace

    def interrupted(*_args):
        raise RuntimeError("interrupted before move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="interrupted before move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    introduced = tmp_path / added
    introduced.parent.mkdir(parents=True, exist_ok=True)
    introduced.write_bytes(b"unreviewed sentinel")
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    with pytest.raises(migration.MigrationError):
        _resume_approved(tmp_path, writers_stopped=True)
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert after == before
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_changed_source_conflict_keeps_journal(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")

    def interrupted(old, new):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    (tmp_path / "drinko_A song.m4a").write_bytes(b"changed")
    with pytest.raises(migration.MigrationError, match="changed"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_resume_after_sidecar_installed_before_old_removed(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    original_unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path.name == "drinko_A song.json":
            raise RuntimeError("power loss after sidecar install")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(RuntimeError, match="power loss"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    assert (tmp_path / "Producer_A song.json").exists()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert _resume_approved(tmp_path, writers_stopped=True) == 1
    assert not (tmp_path / "drinko_A song.json").exists()
    assert len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_destination_created_during_move_is_not_overwritten(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def conflict(old, new):
        if new.suffix == ".m4a":
            new.write_bytes(b"not ours")
        return real(old, new)

    monkeypatch.setattr(migration, "_rename_no_replace", conflict)
    with pytest.raises(FileExistsError):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert (tmp_path / "Producer_A song.m4a").read_bytes() == b"not ours"
    assert (tmp_path / "drinko_A song.m4a").read_bytes() == b"a" * 2048
    assert migration.journal_path(tmp_path).exists()


def test_writer_guard_blocks_incomplete_or_unsafe_migration_metadata(tmp_path):
    migration.raise_if_migration_pending(tmp_path)
    metadata = tmp_path / ".library"
    metadata.mkdir()
    journal = migration.journal_path(tmp_path)
    journal.write_text("incomplete")
    with pytest.raises(migration.MigrationError, match="Pending producer migration"):
        migration.raise_if_migration_pending(tmp_path)
    journal.unlink()
    if sys.platform == "win32":
        return  # Creating symlinks may require Windows privileges.
    journal.symlink_to(tmp_path / "missing")
    with pytest.raises(migration.MigrationError, match="Pending producer migration"):
        migration.raise_if_migration_pending(tmp_path)


@pytest.mark.parametrize("bad", ["safe\u202epm3", "safe\u0085name", "safe\ud800name"])
def test_producer_control_characters_are_rejected(tmp_path, bad):
    recording(tmp_path, "drinko_A song")
    with pytest.raises(migration.MigrationError, match="Invalid producer"):
        migration.plan_migration(tmp_path, {ID1: bad})


def test_title_bidi_control_is_rejected(tmp_path):
    recording(tmp_path, "drinko_A song", title="safe\u202etitle")
    with pytest.raises(migration.MigrationError, match="Invalid title"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})


def test_non_linux_application_fails_closed(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    before = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setattr(migration.sys, "platform", "win32")
    with pytest.raises(migration.MigrationError, match="requires Linux"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_same_filename_sidecar_artist_repaired_and_idempotent(tmp_path):
    recording(tmp_path, "Actual Producer_A song")
    assert _apply_approved(tmp_path, {ID1: "Actual Producer"}, writers_stopped=True) == 1
    sidecar = json.loads((tmp_path / "Actual Producer_A song.json").read_text())
    assert sidecar["artist"] == "Actual Producer"
    assert _apply_approved(tmp_path, {ID1: "Actual Producer"}, writers_stopped=True) == 0


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_same_filename_sidecar_resumes_after_replacement(tmp_path, monkeypatch):
    recording(tmp_path, "Actual Producer_A song")
    original_fsync = migration._fsync_dir
    interrupted = False

    def crash_after_replace(path):
        nonlocal interrupted
        sidecar = tmp_path / "Actual Producer_A song.json"
        if (
            path == tmp_path
            and json.loads(sidecar.read_text())["artist"] == "Actual Producer"
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("interrupted after atomic replacement")
        original_fsync(path)

    monkeypatch.setattr(migration, "_fsync_dir", crash_after_replace)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply_approved(tmp_path, {ID1: "Actual Producer"}, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    monkeypatch.setattr(migration, "_fsync_dir", original_fsync)
    assert _resume_approved(tmp_path, writers_stopped=True) == 1
    assert (
        json.loads((tmp_path / "Actual Producer_A song.json").read_text())["artist"]
        == "Actual Producer"
    )
    assert len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
@pytest.mark.parametrize(
    "interruption", ["none", "before_journal", "before_replace", "after_replace"]
)
def test_real_audio_tagging_and_interrupted_recovery(tmp_path, monkeypatch, interruption):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = tmp_path / "drinko_A song.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x20" * 8000)
    migration._write_artist(original, ".wav", "legacy source handle")
    signature = migration._audio_signature(original)
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [original.name]})
    )
    save = migration._save_journal
    replace = migration.os.replace

    def interrupted_save(journal, plan):
        raise RuntimeError("interrupted before prepared journal")

    def interrupted_replace(old, new):
        if Path(old).suffix == ".part" and Path(new).suffix == ".wav":
            if interruption == "after_replace":
                replace(old, new)
            raise RuntimeError("interrupted around audio replacement")
        return replace(old, new)

    if interruption == "before_journal":
        monkeypatch.setattr(migration, "_save_journal", interrupted_save)
    if interruption in {"before_replace", "after_replace"}:
        monkeypatch.setattr(migration.os, "replace", interrupted_replace)
    if interruption == "none":
        assert _apply_approved(tmp_path, {ID1: "Verified Producer"}, writers_stopped=True) == 1
    else:
        with pytest.raises(RuntimeError, match="interrupted"):
            _apply_approved(tmp_path, {ID1: "Verified Producer"}, writers_stopped=True)
        assert migration.journal_path(tmp_path).exists()
        monkeypatch.setattr(migration, "_save_journal", save)
        monkeypatch.setattr(migration.os, "replace", replace)
        if interruption == "before_journal":
            with pytest.raises(migration.MigrationError, match="Unjournalled audio stage"):
                _resume_approved(tmp_path, writers_stopped=True)
            assert migration.journal_path(tmp_path).exists()
            return
        assert _resume_approved(tmp_path, writers_stopped=True) == 1
    repaired = tmp_path / "Verified Producer_A song.wav"
    assert migration._read_artist(repaired) == "Verified Producer"
    assert migration._audio_signature(repaired) == signature
    assert (
        json.loads((tmp_path / "Verified Producer_A song.json").read_text())["artist"]
        == "Verified Producer"
    )
    assert len(list(tmp_path.glob("*.wav"))) == 1
    assert _apply_approved(tmp_path, {ID1: "Verified Producer"}, writers_stopped=True) == 0


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
@pytest.mark.parametrize("extension,codec", [(".mp3", "libmp3lame")])
def test_real_encoded_audio_keeps_sound_and_other_tags(tmp_path, monkeypatch, extension, codec):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = tmp_path / ("drinko_A song" + extension)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            codec,
            "-metadata",
            "title=Keep this title",
            str(original),
        ],
        capture_output=True,
    )
    if result.returncode:
        pytest.skip("test encoder unavailable")
    from mutagen.id3 import ID3

    ID3(original).save(original, padding=lambda _info: 1024)
    migration._write_artist(original, extension, "legacy")
    signature = migration._audio_signature(original)
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [original.name]})
    )
    assert _apply_approved(tmp_path, {ID1: "Verified Producer"}, writers_stopped=True) == 1
    repaired = tmp_path / ("Verified Producer_A song" + extension)
    assert migration._read_artist(repaired) == "Verified Producer"
    assert migration._audio_signature(repaired) == signature
    from mutagen import File

    assert File(repaired, easy=True)["title"] == ["Keep this title"]
    assert len(list(tmp_path.glob("*" + extension))) == 1


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_audio_extended_attributes_are_refused_without_audio_changes(tmp_path, monkeypatch):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = tmp_path / "drinko_A song.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x20" * 8000)
    try:
        os.setxattr(original, "user.trackie-test", b"do not lose")
    except OSError:
        pytest.skip("filesystem does not support test xattrs")
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [original.name]})
    )
    before = original.read_bytes()
    with pytest.raises(migration.MigrationError, match="extended attributes"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert original.read_bytes() == before
    assert os.getxattr(original, "user.trackie-test") == b"do not lose"
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_unknown_producer_cannot_be_applied(tmp_path, monkeypatch):
    original = recording(tmp_path, "drinko_A song")
    before = original.read_bytes()
    old_sidecar = tmp_path / "drinko_A song.json"
    before_sidecar = old_sidecar.read_bytes()

    def unexpected(*args):
        raise AssertionError("unverified producer must not trigger retagging")

    monkeypatch.setattr(migration, "_artist_matches", unexpected)
    plan = migration.plan_migration(tmp_path, {ID1: None})
    assert plan["groups"][0]["new_sidecar"] == "Unknown artist_A song.json"
    with pytest.raises(migration.MigrationError, match="Unresolved producer"):
        _apply_approved(tmp_path, {ID1: None}, writers_stopped=True)
    assert original.read_bytes() == before
    assert old_sidecar.read_bytes() == before_sidecar
    assert not (tmp_path / "Unknown artist_A song.m4a").exists()
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="atomic migration application requires Linux"
)
def test_embedded_artwork_is_refused_before_any_track_move(tmp_path, monkeypatch):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = tmp_path / "drinko_A song.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x20" * 8000)
    from mutagen.id3 import APIC
    from mutagen.wave import WAVE

    tagged = WAVE(original)
    tagged.add_tags()
    tagged.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=b"artwork"))
    tagged.save()
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [original.name]})
    )
    before = original.read_bytes()
    with pytest.raises(migration.MigrationError):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert original.read_bytes() == before
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.parametrize("suffix", [".lrc", ".txt", ".LRC", ".TXT"])
def test_same_stem_lyrics_without_sidecar_ownership_are_refused(tmp_path, suffix):
    recording(tmp_path, "drinko_A song")
    lyric = tmp_path / ("drinko_A song" + suffix)
    lyric.write_text("user notes")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(migration.MigrationError, match="Unowned cover or lyrics"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert lyric.read_text() == "user notes"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_migration_refuses_busy_archive_before_planning(tmp_path, monkeypatch):
    from suno_dl import archive_lock

    original = recording(tmp_path, "drinko_A song")
    before = original.read_bytes()
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    with (
        archive_lock._exclusive_archive_sync(tmp_path),
        pytest.raises(TimeoutError, match="Archive sync lock busy"),
    ):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert original.read_bytes() == before
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / "Producer_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_migration_holds_common_lock_during_plan_and_moves(tmp_path, monkeypatch):
    from suno_dl import archive_lock

    recording(tmp_path, "drinko_A song")
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    real_plan = migration.plan_migration
    real_rename = migration._rename_no_replace
    observed = []

    def require_locked(stage):
        with (
            pytest.raises(TimeoutError, match="Archive sync lock busy"),
            archive_lock._exclusive_archive_sync(tmp_path),
        ):
            pass
        observed.append(stage)

    def plan(*args, **kwargs):
        require_locked("plan")
        return real_plan(*args, **kwargs)

    def rename(*args, **kwargs):
        require_locked("move")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(migration, "plan_migration", plan)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    assert observed[0] == "plan" and "move" in observed
    with archive_lock._exclusive_archive_sync(tmp_path):
        pass


async def test_migration_refuses_event_loop_before_creating_lock(tmp_path):
    recording(tmp_path, "drinko_A song")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(migration.MigrationError, match="dedicated synchronous process"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_refuses_busy_archive_without_touching_journal(tmp_path, monkeypatch):
    from suno_dl import archive_lock

    original = recording(tmp_path, "drinko_A song")

    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    journal = migration.journal_path(tmp_path)
    before_journal = journal.read_bytes()
    before_audio = original.read_bytes()
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    with (
        archive_lock._exclusive_archive_sync(tmp_path),
        pytest.raises(TimeoutError, match="Archive sync lock busy"),
    ):
        _resume_approved(tmp_path, writers_stopped=True)
    assert journal.read_bytes() == before_journal
    assert original.read_bytes() == before_audio


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
@pytest.mark.parametrize(
    "member", ["drinko_A song.m4a", "drinko_A song.cover.jpeg", "drinko_A song.json"]
)
def test_real_nodump_flag_is_refused_before_migration_writes(tmp_path, member):
    if not shutil.which("chattr") or not shutil.which("lsattr"):
        pytest.skip("filesystem flag utilities unavailable")
    recording(tmp_path, "drinko_A song")
    flagged = tmp_path / member
    command = subprocess.run(["chattr", "+d", str(flagged)], capture_output=True, text=True)
    if command.returncode:
        pytest.skip("filesystem does not support nodump")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="non-default inode flags"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != ".suno-sync.lock"}
    assert after == before
    assert "d" in subprocess.check_output(["lsattr", "-d", str(flagged)], text=True).split()[0]
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_unavailable_inode_ioctl_fails_closed_before_migration_writes(tmp_path, monkeypatch):
    import errno
    import fcntl

    recording(tmp_path, "drinko_A song")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.ENOTTY, "ioctl unavailable")

    monkeypatch.setattr(fcntl, "ioctl", unsupported)
    with pytest.raises(migration.MigrationError, match="Cannot verify inode flags"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != ".suno-sync.lock"
    } == before
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_staged_sidecar_with_different_default_flags_refuses_replacement(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._inode_flags

    def differ(path):
        flags = real(path)
        return flags ^ migration._FS_EXTENT_FL if path.name.startswith("sidecar-") else flags

    monkeypatch.setattr(migration, "_inode_flags", differ)
    with pytest.raises(migration.MigrationError, match="staging flags differ"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    assert (tmp_path / "drinko_A song.json").exists()
    assert not (tmp_path / "Producer_A song.json").exists()
    assert len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_staged_audio_with_different_default_flags_refuses_replacement(tmp_path, monkeypatch):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = tmp_path / "drinko_A song.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x20" * 8000)
    before = original.read_bytes()
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [original.name]})
    )
    real = migration._inode_flags

    def differ(path):
        flags = real(path)
        return flags ^ migration._FS_EXTENT_FL if path.suffix == ".part" else flags

    monkeypatch.setattr(migration, "_inode_flags", differ)
    with pytest.raises(migration.MigrationError, match="staging flags differ"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    moved = tmp_path / "Producer_A song.wav"
    assert moved.read_bytes() == before
    assert not (tmp_path / "Producer_A song.json").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_old_journal_without_verified_flags_fails_closed(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")

    def interrupted(*_args):
        raise RuntimeError("interrupt")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="interrupt"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    plan["version"] = 1
    journal.write_text(json.dumps(plan))
    with pytest.raises(migration.MigrationError, match="Invalid migration journal"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert journal.exists()
    assert (tmp_path / "drinko_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_missing_filesystem_specific_ioctl_fails_closed(tmp_path, monkeypatch):
    import errno
    import fcntl

    recording(tmp_path, "drinko_A song")
    real = fcntl.ioctl

    def unsupported(fd, request, result, mutate):
        if request == migration._FS_IOC_FSGETXATTR:
            raise OSError(errno.ENOTTY, "fsx query unavailable")
        return real(fd, request, result, mutate)

    monkeypatch.setattr(fcntl, "ioctl", unsupported)
    with pytest.raises(migration.MigrationError, match="Cannot verify inode flags"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()
    assert (tmp_path / "drinko_A song.m4a").read_bytes() == b"a" * 2048


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
@pytest.mark.parametrize("field_offset", [0, 4, 12, 16])
def test_nondefault_filesystem_specific_attributes_fail_closed(tmp_path, monkeypatch, field_offset):
    import fcntl

    recording(tmp_path, "drinko_A song")
    real = fcntl.ioctl

    def nondefault(fd, request, result, mutate):
        if request == migration._FS_IOC_FSGETXATTR:
            result[field_offset : field_offset + 4] = (1).to_bytes(4, sys.byteorder)
            return 0
        return real(fd, request, result, mutate)

    monkeypatch.setattr(fcntl, "ioctl", nondefault)
    with pytest.raises(migration.MigrationError, match="non-default inode flags or attributes"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only inode flags")
def test_readonly_extent_count_does_not_block_normal_file(tmp_path, monkeypatch):
    import fcntl

    recording(tmp_path, "drinko_A song")
    real = fcntl.ioctl

    def extent_count(fd, request, result, mutate):
        value = real(fd, request, result, mutate)
        if request == migration._FS_IOC_FSGETXATTR:
            result[8:12] = (123).to_bytes(4, sys.byteorder)
        return value

    monkeypatch.setattr(fcntl, "ioctl", extent_count)
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "setxattr"),
    reason="Linux xattrs required",
)
@pytest.mark.parametrize("already_installed", [False, True])
def test_resume_refuses_added_sidecar_xattr_and_preserves_files(
    tmp_path, monkeypatch, already_installed
):
    recording(tmp_path, "drinko_A song")
    old = tmp_path / "drinko_A song.json"
    before = old.read_bytes()
    real_rename = migration._rename_no_replace
    real_unlink = Path.unlink
    if already_installed:

        def interrupt_unlink(path, *args, **kwargs):
            if path == old:
                raise RuntimeError("interrupt before old sidecar unlink")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", interrupt_unlink)
    else:

        def interrupt_rename(*_args):
            raise RuntimeError("interrupt before rename")

        monkeypatch.setattr(migration, "_rename_no_replace", interrupt_rename)
    with pytest.raises(RuntimeError, match="interrupt"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(migration, "_rename_no_replace", real_rename)
    try:
        os.setxattr(old, "user.review_test", b"preserve")
    except OSError:
        pytest.skip("filesystem does not support test xattrs")
    new = tmp_path / "Producer_A song.json"
    new_before = new.read_bytes() if new.exists() else None
    journal = migration.journal_path(tmp_path)
    with pytest.raises(migration.MigrationError, match="Sidecar.*changed|extended attributes"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert old.read_bytes() == before
    assert os.getxattr(old, "user.review_test") == b"preserve"
    assert journal.exists()
    assert (new.read_bytes() if new.exists() else None) == new_before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("already_installed", [False, True])
def test_resume_refuses_changed_sidecar_mode_and_preserves_files(
    tmp_path, monkeypatch, already_installed
):
    recording(tmp_path, "drinko_A song")
    old = tmp_path / "drinko_A song.json"
    os.chmod(old, 0o600)
    before = old.read_bytes()
    real_rename = migration._rename_no_replace
    real_unlink = Path.unlink
    if already_installed:

        def interrupt_unlink(path, *args, **kwargs):
            if path == old:
                raise RuntimeError("interrupt before old sidecar unlink")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", interrupt_unlink)
    else:

        def interrupt_rename(*_args):
            raise RuntimeError("interrupt before rename")

        monkeypatch.setattr(migration, "_rename_no_replace", interrupt_rename)
    with pytest.raises(RuntimeError, match="interrupt"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(migration, "_rename_no_replace", real_rename)
    os.chmod(old, 0o640)
    new = tmp_path / "Producer_A song.json"
    new_before = new.read_bytes() if new.exists() else None
    with pytest.raises(migration.MigrationError, match="Sidecar.*metadata changed"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert old.read_bytes() == before and old.stat().st_mode & 0o777 == 0o640
    assert migration.journal_path(tmp_path).exists()
    assert (new.read_bytes() if new.exists() else None) == new_before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_sidecar_metadata_rechecked_immediately_before_unlink(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    old = tmp_path / "drinko_A song.json"
    new = tmp_path / "Producer_A song.json"
    original_sync = migration._fsync_dir
    changed = False

    def alter_after_install(path):
        nonlocal changed
        original_sync(path)
        if path == tmp_path and old.exists() and new.exists() and not changed:
            os.chmod(old, 0o640)
            changed = True

    monkeypatch.setattr(migration, "_fsync_dir", alter_after_install)
    with pytest.raises(migration.MigrationError, match="Sidecar.*metadata changed"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert changed and old.exists() and new.exists()
    assert old.stat().st_mode & 0o777 == 0o640
    assert migration.journal_path(tmp_path).exists()


def _real_wav_for_migration(root):
    path = root / "drinko_A song.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x20" * 8000)
    (root / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [path.name]})
    )
    return path


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("case", ["same_second", "float_rounding", "future_original"])
def test_retagged_audio_changes_actual_ma_integer_mtime(tmp_path, monkeypatch, case):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = _real_wav_for_migration(tmp_path)
    if case == "same_second":
        original_ns, clock_ns = 1_700_000_000_250_000_000, 1_700_000_000_750_000_000
    elif case == "float_rounding":
        original_ns, clock_ns = 1_700_000_000_999_999_999, 1_700_000_001_000_000_100
    else:
        original_ns, clock_ns = 1_900_000_000_000_000_000, 1_700_000_000_500_000_000
    os.utime(original, ns=(original_ns, original_ns))
    before = original.stat()
    if case == "float_rounding":
        assert int(before.st_mtime) != before.st_mtime_ns // 1_000_000_000
    monkeypatch.setattr(migration.time, "time_ns", lambda: clock_ns)
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    new = tmp_path / "Producer_A song.wav"
    after = new.stat()
    assert int(after.st_mtime) != int(before.st_mtime)
    assert after.st_mtime_ns <= clock_ns
    if case == "future_original":
        assert after.st_mtime_ns < before.st_mtime_ns


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_sparse_wav_is_refused_without_materializing_or_creating_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    audio = tmp_path / "drinko_A song.wav"
    size = 4 * 1024 * 1024
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        8000,
        16000,
        2,
        16,
        b"data",
        size,
    )
    with audio.open("wb") as stream:
        stream.write(header)
        stream.seek(len(header) + size - 1)
        stream.write(b"\x00")
    before = audio.stat()
    if not hasattr(before, "st_blocks") or before.st_blocks * 512 >= before.st_size:
        pytest.skip("filesystem did not create a detectable sparse file")
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [audio.name]})
    )
    with pytest.raises(migration.MigrationError, match="Sparse or compressed"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    after = audio.stat()
    assert after.st_size == before.st_size and after.st_blocks == before.st_blocks
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_insufficient_staging_space_refuses_before_journal(tmp_path, monkeypatch):
    original = recording(tmp_path, "drinko_A song")
    before = original.read_bytes()
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda _path: usage_type(0, 0, 0))
    with pytest.raises(migration.MigrationError, match="Insufficient staging space"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert original.read_bytes() == before
    assert not (tmp_path / ".library").exists()
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("drift", ["mode", "xattr"])
def test_resume_refuses_installed_audio_metadata_drift(tmp_path, monkeypatch, drift):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    if drift == "xattr" and not hasattr(os, "setxattr"):
        pytest.skip("xattrs unavailable")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    original = _real_wav_for_migration(tmp_path)
    os.chmod(original, 0o600)
    target = tmp_path / "Producer_A song.wav"
    real_replace = migration.os.replace

    def interrupted(source, destination):
        real_replace(source, destination)
        if Path(destination) == target:
            raise RuntimeError("interrupted after audio replacement")

    monkeypatch.setattr(migration.os, "replace", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration.os, "replace", real_replace)
    before = target.read_bytes()
    if drift == "mode":
        os.chmod(target, 0o666)  # noqa: S103 - disposable metadata-drift fixture
    else:
        try:
            os.setxattr(target, "user.review_test", b"preserve")
        except OSError:
            pytest.skip("filesystem does not support test xattrs")
    with pytest.raises(migration.MigrationError):
        _resume_approved(tmp_path, writers_stopped=True)
    assert target.read_bytes() == before and migration.journal_path(tmp_path).exists()
    if drift == "mode":
        assert target.stat().st_mode & 0o777 == 0o666
    else:
        assert os.getxattr(target, "user.review_test") == b"preserve"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_preallocated_beyond_eof_does_not_hide_sparse_wav(tmp_path, monkeypatch):
    if not shutil.which("fallocate") or not hasattr(os, "SEEK_HOLE"):
        pytest.skip("fallocate or SEEK_HOLE unavailable")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    audio = tmp_path / "drinko_A song.wav"
    size = 4 * 1024 * 1024
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        44100,
        88200,
        2,
        16,
        b"data",
        size,
    )
    with audio.open("wb") as stream:
        stream.write(header)
        stream.seek(len(header) + size - 1)
        stream.write(b"\x00")
    completed = subprocess.run(
        [
            "fallocate",
            "--keep-size",
            "--offset",
            str(2 * size),
            "--length",
            str(2 * size),
            str(audio),
        ],
        capture_output=True,
        check=False,
    )
    before = audio.stat()
    if completed.returncode or before.st_blocks * 512 < before.st_size:
        pytest.skip("filesystem cannot preallocate past EOF")
    with audio.open("rb") as stream:
        try:
            first_hole = os.lseek(stream.fileno(), 0, os.SEEK_HOLE)
        except OSError:
            pytest.skip("filesystem does not implement SEEK_HOLE")
    if first_hole >= before.st_size:
        pytest.skip("filesystem does not expose the hole")
    (tmp_path / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": None, "saved_files": [audio.name]})
    )
    with pytest.raises(migration.MigrationError, match="Sparse or compressed"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    after = audio.stat()
    assert after.st_size == before.st_size and after.st_blocks == before.st_blocks
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_unverifiable_hole_map_is_refused_before_journal(tmp_path, monkeypatch):
    recording(tmp_path, ID1)
    real_lseek = os.lseek

    def unsupported(fd, offset, whence):
        if whence == os.SEEK_HOLE:
            raise OSError("injected unsupported SEEK_HOLE")
        return real_lseek(fd, offset, whence)

    monkeypatch.setattr(migration.os, "lseek", unsupported)
    with pytest.raises(migration.MigrationError, match="Cannot verify source holes"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_rejects_planted_sidecar_with_matching_bytes(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def interrupt(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    plan = json.loads(migration.journal_path(tmp_path).read_text())
    group = plan["groups"][0]
    planted = tmp_path / group["new_sidecar"]
    planted.write_text(group["new_content"])
    planted.chmod(group["sidecar_mode"])
    with pytest.raises(migration.MigrationError, match="Unowned sidecar"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert planted.read_text() == group["new_content"]
    assert (tmp_path / group["old_sidecar"]).exists()
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("boundary", ["before_journal", "after_journal", "after_install"])
def test_sidecar_prepared_inode_journal_interruption(tmp_path, monkeypatch, boundary):
    recording(tmp_path, "drinko_A song")
    save = migration._save_journal
    rename = migration._rename_no_replace

    def interrupted_save(journal, plan):
        if any(group.get("sidecar_prepared") for group in plan["groups"]):
            if boundary == "after_journal":
                save(journal, plan)
            raise RuntimeError("sidecar preparation interruption")
        save(journal, plan)

    def interrupted_rename(old, new):
        if Path(old).name.startswith("sidecar-"):
            rename(old, new)
            raise RuntimeError("sidecar installation interruption")
        rename(old, new)

    if boundary in ("before_journal", "after_journal"):
        monkeypatch.setattr(migration, "_save_journal", interrupted_save)
    else:
        monkeypatch.setattr(migration, "_rename_no_replace", interrupted_rename)
    with pytest.raises(RuntimeError, match="interruption"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_save_journal", save)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    if boundary == "before_journal":
        with pytest.raises(migration.MigrationError, match="Unjournalled sidecar"):
            _resume_approved(tmp_path, writers_stopped=True)
        assert migration.journal_path(tmp_path).exists()
        assert list((tmp_path / ".library").glob(".producer-tag-*/sidecar-*.part"))
    else:
        assert _resume_approved(tmp_path, writers_stopped=True) == 1
        assert (tmp_path / "Producer_A song.json").exists()
        assert not (tmp_path / "drinko_A song.json").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_recovered_audio_requires_parent_fsync_before_journal_cleanup(tmp_path, monkeypatch):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    _real_wav_for_migration(tmp_path)
    target = tmp_path / "Producer_A song.wav"
    replace = migration.os.replace

    def interrupt_after_install(old, new):
        replace(old, new)
        if Path(new) == target:
            raise RuntimeError("audio installed")

    monkeypatch.setattr(migration.os, "replace", interrupt_after_install)
    with pytest.raises(RuntimeError, match="audio installed"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration.os, "replace", replace)
    retag = migration._retag_one
    sync = migration._fsync_dir
    in_retag = False

    def tracked_retag(*args):
        nonlocal in_retag
        in_retag = True
        try:
            return retag(*args)
        finally:
            in_retag = False

    def fail_recovered_audio_sync(path):
        if in_retag and path == tmp_path:
            raise OSError("cannot sync recovered audio directory")
        sync(path)

    monkeypatch.setattr(migration, "_retag_one", tracked_retag)
    monkeypatch.setattr(migration, "_fsync_dir", fail_recovered_audio_sync)
    with pytest.raises(OSError, match="cannot sync"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    monkeypatch.setattr(migration, "_fsync_dir", sync)
    assert _resume_approved(tmp_path, writers_stopped=True) == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_recovered_sidecar_unlink_requires_parent_fsync(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    old = tmp_path / "drinko_A song.json"
    unlink = Path.unlink

    def interrupt_after_unlink(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == old:
            raise RuntimeError("old sidecar unlinked")

    monkeypatch.setattr(Path, "unlink", interrupt_after_unlink)
    with pytest.raises(RuntimeError, match="old sidecar unlinked"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(Path, "unlink", unlink)
    sync = migration._fsync_dir

    def fail_directory(path):
        if path == tmp_path:
            raise OSError("cannot sync recovered sidecar directory")
        sync(path)

    monkeypatch.setattr(migration, "_fsync_dir", fail_directory)
    with pytest.raises(OSError, match="cannot sync"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert migration.journal_path(tmp_path).exists()
    monkeypatch.setattr(migration, "_fsync_dir", sync)
    assert _resume_approved(tmp_path, writers_stopped=True) == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_recovery_does_not_unlink_foreign_unjournalled_audio_stage(tmp_path, monkeypatch):
    pytest.importorskip("mutagen")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    _real_wav_for_migration(tmp_path)
    save = migration._save_journal

    def interrupt_before_prepared_record(_journal, _plan):
        raise RuntimeError("before audio journal")

    monkeypatch.setattr(migration, "_save_journal", interrupt_before_prepared_record)
    with pytest.raises(RuntimeError, match="before audio journal"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_save_journal", save)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    tag = plan["groups"][0]["tags"][0]
    stage = tmp_path / plan["staging"]["path"] / tag["stage_name"]
    assert stage.exists()
    stage.unlink()
    stage.write_bytes(b"foreign sentinel")
    before = stage.stat().st_ino
    with pytest.raises(migration.MigrationError, match="Unjournalled audio stage"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert stage.read_bytes() == b"foreign sentinel"
    assert stage.stat().st_ino == before
    assert journal.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("boundary", ["before_completion", "after_completion", "after_unlink"])
def test_completion_recovery_after_real_process_exit(tmp_path, boundary):
    recording(tmp_path, "drinko_A song")
    script = """
import os, sys
from pathlib import Path
from suno_dl import producer_migration as m
root = Path(sys.argv[1])
boundary = sys.argv[2]
m._artist_matches = lambda *_: True
publish = m._publish_completion
unlink = Path.unlink
def interrupted_publish(*args):
    if boundary == 'before_completion':
        os._exit(43)
    publish(*args)
    if boundary == 'after_completion':
        os._exit(43)
def interrupted_unlink(path, *args, **kwargs):
    unlink(path, *args, **kwargs)
    if boundary == 'after_unlink' and path == m.journal_path(root):
        os._exit(43)
m._publish_completion = interrupted_publish
Path.unlink = interrupted_unlink
mapping = {sys.argv[3]: 'Producer'}
plan = m.plan_migration(root, mapping)
m.apply_migration(root, mapping, writers_stopped=True,
                  producer_mapping_reviewed=True, reviewed_producers=mapping,
                  reviewed_plan_sha256=m.review_plan_digest(plan))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), boundary, ID1],
        check=False,
        capture_output=True,
    )
    assert child.returncode == 43, child.stderr.decode(errors="replace")
    journal = migration.journal_path(tmp_path)
    if boundary == "before_completion":
        assert journal.exists()
        assert _resume_approved(tmp_path, writers_stopped=True) == 1
    elif boundary == "after_completion":
        assert journal.exists()
        migration.raise_if_migration_pending(tmp_path)
        assert _resume_approved(tmp_path, writers_stopped=True) == 0
    else:
        assert not journal.exists()
        migration.raise_if_migration_pending(tmp_path)
    assert list((tmp_path / ".library").glob("producer-completed-*.json"))
    assert (tmp_path / "Producer_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_stale_completed_journal_never_replays_after_later_write(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    journal = migration.journal_path(tmp_path)
    original_unlink = Path.unlink
    saved = []

    def capture(path, *args, **kwargs):
        if path == journal:
            saved.append(path.read_bytes())
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", capture)
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    monkeypatch.setattr(Path, "unlink", original_unlink)
    media = tmp_path / "Producer_A song.m4a"
    media.write_bytes(b"later independent write")
    journal.write_bytes(saved[0])
    migration.raise_if_migration_pending(tmp_path)
    assert _resume_approved(tmp_path, writers_stopped=True) == 0
    assert media.read_bytes() == b"later independent write"
    assert not journal.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("corruption", ["malformed", "duplicate", "mismatch"])
def test_corrupt_or_mismatched_completion_cannot_authorize_stale_journal(
    tmp_path, monkeypatch, corruption
):
    recording(tmp_path, "drinko_A song")
    journal = migration.journal_path(tmp_path)
    unlink = Path.unlink
    saved = []

    def capture(path, *args, **kwargs):
        if path == journal:
            saved.append(path.read_bytes())
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", capture)
    _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(Path, "unlink", unlink)
    journal.write_bytes(saved[0])
    completion = next((tmp_path / ".library").glob("producer-completed-*.json"))
    record = json.loads(completion.read_text())
    if corruption == "malformed":
        completion.write_text("not json")
    elif corruption == "duplicate":
        raw = completion.read_text()
        completion.write_text(raw[:-2] + ', "id": "' + record["id"] + '"}')
    else:
        record["journal_sha256"] = "0" * 64
        completion.write_text(json.dumps(record))
    with pytest.raises(migration.MigrationError):
        migration.raise_if_migration_pending(tmp_path)
    assert journal.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_completion_id_collision_fails_before_journal_creation(tmp_path, monkeypatch):
    import uuid

    recording(tmp_path, "drinko_A song")
    fixed = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    metadata = tmp_path / ".library"
    metadata.mkdir()
    info = tmp_path.stat()
    completion = metadata / f"producer-completed-{fixed}.json"
    completion.write_text(
        json.dumps(
            {
                "version": 1,
                "id": str(fixed),
                "root_dev": info.st_dev,
                "root_ino": info.st_ino,
                "journal_sha256": "0" * 64,
            }
        )
    )
    before = completion.read_bytes()
    monkeypatch.setattr(migration.uuid, "uuid4", lambda: fixed)
    with pytest.raises(migration.MigrationError, match="identity already exists"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert completion.read_bytes() == before
    assert not migration.journal_path(tmp_path).exists()
    assert (tmp_path / "drinko_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("boundary", ["completion", "unlink"])
def test_completion_sync_failure_remains_recoverable(tmp_path, monkeypatch, boundary):
    recording(tmp_path, "drinko_A song")
    journal = migration.journal_path(tmp_path)
    original_sync = migration._fsync_dir
    fired = False

    def fail_after_change(path):
        nonlocal fired
        completed = list((tmp_path / ".library").glob("producer-completed-*.json"))
        if (
            not fired
            and path == journal.parent
            and completed
            and (journal.exists() == (boundary == "completion"))
        ):
            fired = True
            raise OSError("injected completion sync failure")
        original_sync(path)

    monkeypatch.setattr(migration, "_fsync_dir", fail_after_change)
    with pytest.raises(OSError, match="completion sync"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    assert fired
    monkeypatch.setattr(migration, "_fsync_dir", original_sync)
    migration.raise_if_migration_pending(tmp_path)
    if journal.exists():
        assert _resume_approved(tmp_path, writers_stopped=True) == 0
    assert (tmp_path / "Producer_A song.m4a").exists()
    assert list(journal.parent.glob("producer-completed-*.json"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("missing", ["assertion", "mapping", "plan", "truthy"])
def test_apply_requires_explicit_plan_and_mapping_review(tmp_path, missing):
    recording(tmp_path, "drinko_A song")
    mapping = {ID1: "Producer"}
    plan = migration.plan_migration(tmp_path, mapping)
    args = {
        "writers_stopped": True,
        "producer_mapping_reviewed": True,
        "reviewed_producers": dict(mapping),
        "reviewed_plan_sha256": migration.review_plan_digest(plan),
    }
    if missing == "assertion":
        del args["producer_mapping_reviewed"]
    elif missing == "mapping":
        del args["reviewed_producers"]
    elif missing == "plan":
        del args["reviewed_plan_sha256"]
    else:
        args["producer_mapping_reviewed"] = 1
    with pytest.raises(migration.MigrationError, match="review"):
        migration.apply_migration(tmp_path, mapping, **args)
    assert not migration.journal_path(tmp_path).exists()
    assert (tmp_path / "drinko_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_apply_rejects_changed_mapping_and_plan(tmp_path):
    original = recording(tmp_path, "drinko_A song")
    mapping = {ID1: "Producer"}
    plan = migration.plan_migration(tmp_path, mapping)
    digest = migration.review_plan_digest(plan)
    with pytest.raises(migration.MigrationError, match="mapping does not match"):
        migration.apply_migration(
            tmp_path,
            mapping,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers={ID1: "Other"},
            reviewed_plan_sha256=digest,
        )
    original.write_bytes(b"b" * 2048)
    with pytest.raises(migration.MigrationError, match="plan differs"):
        migration.apply_migration(
            tmp_path,
            mapping,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers=dict(mapping),
            reviewed_plan_sha256=digest,
        )
    assert original.read_bytes() == b"b" * 2048
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_requires_independent_exact_journal_review(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def interrupt(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    journal = migration.journal_path(tmp_path)
    before = journal.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    with pytest.raises(migration.MigrationError, match="review"):
        migration.resume_migration(tmp_path, writers_stopped=True)
    with pytest.raises(migration.MigrationError, match="review"):
        migration.resume_migration(tmp_path, writers_stopped=True, producer_mapping_reviewed=True)
    with pytest.raises(migration.MigrationError, match="differs"):
        migration.resume_migration(
            tmp_path,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_journal_sha256="0" * 64,
        )
    journal.write_bytes(before + b" ")
    with pytest.raises(migration.MigrationError, match="differs"):
        migration.resume_migration(
            tmp_path,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_journal_sha256=digest,
        )
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert not (tmp_path / "Producer_A song.m4a").exists()
    assert journal.read_bytes() == before + b" "


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize(
    "artist",
    [
        None,
        "",
        " ",
        "Unknown artist",
        "UNKNOWN",
        "unresolved-producer",
        "N/A",
        "none",
        "TBD",
        "null",
        "unverified",
        "un\u200bknown",
        "Ｎ／Ａ",
        "not verified",
        "?",
    ],
)
def test_apply_refuses_unresolved_decisions_before_archive_writes(tmp_path, artist):
    original = recording(tmp_path, "drinko_A song")
    mapping = {ID1: artist}
    with pytest.raises(migration.MigrationError):
        migration.apply_migration(
            tmp_path,
            mapping,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers=dict(mapping),
            reviewed_plan_sha256="0" * 64,
        )
    assert original.exists()
    assert not (tmp_path / ".library").exists()
    assert not (tmp_path / ".suno-sync.lock").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("decision", [None, "Unknown artist"])
def test_resume_refuses_unresolved_recorded_producer(tmp_path, monkeypatch, decision):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def interrupted(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    plan["groups"][0]["producer_decision"] = decision
    journal.write_text(json.dumps(plan))
    before = journal.read_bytes()
    with pytest.raises(migration.MigrationError, match="Unresolved migration producer"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert journal.read_bytes() == before
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert not (tmp_path / "Producer_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_refuses_untrusted_old_journal_without_producer_decision(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def interrupted(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    plan["version"] = 6
    del plan["groups"][0]["producer_decision"]
    journal.write_text(json.dumps(plan))
    with pytest.raises(migration.MigrationError, match="Legacy migration journal"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert journal.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("suffix", [".m4a", ".cover.jpeg"])
def test_reviewed_plan_binds_content_even_when_inode_size_and_mtime_are_restored(tmp_path, suffix):
    recording(tmp_path, "drinko_A song")
    mapping = {ID1: "Producer"}
    plan = migration.plan_migration(tmp_path, mapping)
    digest = migration.review_plan_digest(plan)
    path = tmp_path / ("drinko_A song" + suffix)
    before = path.stat()
    old_snapshot = next(item for item in plan["reviewed_files"] if item["path"] == path.name)
    replacement = b"z" * before.st_size
    path.write_bytes(replacement)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    changed = migration.plan_migration(tmp_path, mapping)
    new_snapshot = next(item for item in changed["reviewed_files"] if item["path"] == path.name)
    assert old_snapshot["sha256"] != new_snapshot["sha256"]
    with pytest.raises(migration.MigrationError, match="plan differs"):
        migration.apply_migration(
            tmp_path,
            mapping,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers=dict(mapping),
            reviewed_plan_sha256=digest,
        )
    assert path.read_bytes() == replacement
    assert not migration.journal_path(tmp_path).exists()
    assert not (tmp_path / ("Producer_A song" + suffix)).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_reviewed_plan_binds_relevant_file_mode(tmp_path):
    media = recording(tmp_path, "drinko_A song")
    mapping = {ID1: "Producer"}
    plan = migration.plan_migration(tmp_path, mapping)
    before = media.stat()
    media.chmod(0o600)
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(migration.MigrationError, match="plan differs"):
        migration.apply_migration(
            tmp_path,
            mapping,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers=dict(mapping),
            reviewed_plan_sha256=migration.review_plan_digest(plan),
        )
    assert media.stat().st_mode & 0o777 == 0o600
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("phase", ["before_move", "after_first_move", "unchanged_names"])
@pytest.mark.parametrize("member", ["media", "cover"])
def test_resume_refuses_reviewed_member_rewrite_even_if_mtime_restored(
    tmp_path, monkeypatch, phase, member
):
    stem = "Producer_A song" if phase == "unchanged_names" else "drinko_A song"
    recording(tmp_path, stem)
    mapping = {ID1: "Producer"}
    rename = migration._rename_no_replace
    save = migration._save_journal
    count = 0

    def interrupt_rename(source, dest):
        nonlocal count
        count += 1
        if count == (1 if phase == "before_move" else 2):
            raise RuntimeError("interrupted member move")
        return rename(source, dest)

    def interrupt_saved_sidecar(journal, plan):
        save(journal, plan)
        if any(group.get("sidecar_prepared") for group in plan["groups"]):
            raise RuntimeError("interrupted sidecar stage")

    if phase == "unchanged_names":
        monkeypatch.setattr(migration, "_save_journal", interrupt_saved_sidecar)
    else:
        monkeypatch.setattr(migration, "_rename_no_replace", interrupt_rename)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply_approved(tmp_path, mapping, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    monkeypatch.setattr(migration, "_save_journal", save)
    suffix = ".m4a" if member == "media" else ".cover.jpeg"
    old = tmp_path / (stem + suffix)
    new = tmp_path / ("Producer_A song" + suffix)
    path = old if old.exists() else new
    stat_before = path.stat()
    changed = b"z" * stat_before.st_size
    path.write_bytes(changed)
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) == (
        stat_before.st_ino,
        stat_before.st_size,
        stat_before.st_mtime_ns,
    )
    names = {item.name for item in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Reviewed file changed"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert path.read_bytes() == changed
    assert {item.name for item in tmp_path.iterdir()} == names
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_refuses_reviewed_member_mode_change_before_other_moves(tmp_path, monkeypatch):
    recording(tmp_path, "drinko_A song")
    rename = migration._rename_no_replace

    def interrupt(*_args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    cover = tmp_path / "drinko_A song.cover.jpeg"
    cover.chmod(0o600)
    names = {item.name for item in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Reviewed file changed"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert {item.name for item in tmp_path.iterdir()} == names
    assert cover.stat().st_mode & 0o777 == 0o600
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
def test_resume_preflights_later_group_sidecar_before_any_moves(tmp_path, monkeypatch):
    recording(tmp_path, "first", ID1)
    recording(tmp_path, "second", ID2)
    rename = migration._rename_no_replace

    def interrupt(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer", ID2: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    sidecar = tmp_path / "second.json"
    before = sidecar.stat()
    original = sidecar.read_text()
    changed = original.replace("untrusted legacy", "different legacy")
    assert len(changed) == len(original) and changed != original
    sidecar.write_text(changed)
    os.utime(sidecar, ns=(before.st_atime_ns, before.st_mtime_ns))
    names = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError, match="Sidecar.*changed"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert {path.name for path in tmp_path.iterdir()} == names
    assert sidecar.read_text() == changed
    assert (tmp_path / "first.m4a").exists()
    assert (tmp_path / "first.json").exists()
    assert migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("corruption", ["missing_member", "wrong_destination", "missing_sidecar"])
def test_resume_rejects_journal_mappings_that_disagree_before_moves(
    tmp_path, monkeypatch, corruption
):
    recording(tmp_path, "drinko_A song")
    rename = migration._rename_no_replace

    def interrupt(*_args):
        raise RuntimeError("before first move")

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    with pytest.raises(RuntimeError, match="before first move"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    if corruption == "missing_member":
        plan["groups"][0]["reviewed_members"].pop()
    elif corruption == "wrong_destination":
        plan["groups"][0]["reviewed_members"][0]["new"] = "unexpected.m4a"
    else:
        old_sidecar = plan["groups"][0]["old_sidecar"]
        plan["reviewed_files"] = [
            item for item in plan["reviewed_files"] if item["path"] != old_sidecar
        ]
    journal.write_text(json.dumps(plan))
    names = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(migration.MigrationError):
        _resume_approved(tmp_path, writers_stopped=True)
    assert {path.name for path in tmp_path.iterdir()} == names
    assert (tmp_path / "drinko_A song.m4a").exists()
    assert journal.exists()


def _m4a_with_opaque_locale(root, extension, artist):
    from mutagen.mp4 import MP4

    path = root / ("drinko_A song" + extension)
    shutil.copyfile(Path(__file__).parent / "fixtures" / "tiny.m4a", path)
    audio = MP4(path)
    audio["zzzz"] = ["sentinel"]
    audio["\u00a9ART"] = [artist]
    audio.save()
    content = bytearray(path.read_bytes())
    atom = content.index(b"zzzz")
    data = content.index(b"data", atom)
    content[data + 8 : data + 12] = (42).to_bytes(4, "big")
    path.write_bytes(content)
    (root / "drinko_A song.json").write_text(
        json.dumps({"id": ID1, "title": "A song", "artist": "Old", "saved_files": [path.name]})
    )
    return path


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("extension", [".m4a", ".mp4"])
def test_mp4_retag_preserves_opaque_locale(tmp_path, monkeypatch, extension):
    pytest.importorskip("mutagen")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    audio = _m4a_with_opaque_locale(tmp_path, extension, "Old")
    before = audio.read_bytes()
    pos = before.index(b"zzzz") - 4
    size = int.from_bytes(before[pos : pos + 4], "big")
    opaque = before[pos : pos + size]
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    target = tmp_path / ("Producer_A song" + extension)
    assert migration._read_artist(target) == "Producer"
    assert opaque in target.read_bytes()
    assert not audio.exists() and not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("extension", [".m4a", ".mp4"])
def test_mp4_rename_with_already_matching_artist_preserves_all_bytes(
    tmp_path, monkeypatch, extension
):
    pytest.importorskip("mutagen")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    audio = _m4a_with_opaque_locale(tmp_path, extension, "Producer")
    before = audio.read_bytes()
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    assert not audio.exists()
    assert (tmp_path / ("Producer_A song" + extension)).read_bytes() == before
    assert not migration.journal_path(tmp_path).exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("disguised", [False, True])
def test_old_pending_mp4_retag_journal_is_blocked_before_recovery(tmp_path, monkeypatch, disguised):
    pytest.importorskip("mutagen")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    audio = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    sidecar = tmp_path / "drinko_A song.json"
    guard = migration._require_supported_tag_updates
    rename = migration._rename_no_replace

    def interrupted(*_args):
        raise RuntimeError("simulated old journal before move")

    monkeypatch.setattr(migration, "_require_supported_tag_updates", lambda _plan: None)
    monkeypatch.setattr(migration, "_rename_no_replace", interrupted)
    with pytest.raises(RuntimeError, match="simulated old journal"):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_require_supported_tag_updates", guard)
    monkeypatch.setattr(migration, "_rename_no_replace", rename)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    if disguised:
        plan["groups"][0]["tags"][0]["extension"] = ".wav"
    else:
        plan["version"] = 8
    journal.write_text(json.dumps(plan))
    before = (audio.read_bytes(), sidecar.read_bytes(), journal.read_bytes())
    expected = "Invalid migration tag extension" if disguised else "Legacy migration journal"
    with pytest.raises(migration.MigrationError, match=expected):
        _resume_approved(tmp_path, writers_stopped=True)
    assert (audio.read_bytes(), sidecar.read_bytes(), journal.read_bytes()) == before
    assert not (tmp_path / "Producer_A song.m4a").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only migration")
@pytest.mark.parametrize("extension", [".wav", ".mp3"])
def test_other_artist_updates_preserve_nonartist_frames(tmp_path, monkeypatch, extension):
    pytest.importorskip("mutagen")
    monkeypatch.setattr(migration, "_artist_matches", _REAL_ARTIST_MATCHES)
    if extension == ".wav":
        audio = _real_wav_for_migration(tmp_path)
    else:
        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg is required")
        from mutagen.id3 import ID3, TPE1, TXXX

        audio = tmp_path / "drinko_A song.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "libmp3lame",
                str(audio),
            ],
            check=True,
        )
        tags = ID3(audio)
        tags.add(TXXX(encoding=3, desc="Sentinel", text=["hello"]))
        tags.add(TPE1(encoding=3, text=["Old"]))
        tags.save(audio)
        content = bytearray(audio.read_bytes())
        index = content.index(b"TXXX")
        content[index + 8] = 0x40
        audio.write_bytes(content)
        (tmp_path / "drinko_A song.json").write_text(
            json.dumps({"id": ID1, "title": "A song", "saved_files": [audio.name]})
        )
    sidecar = tmp_path / "drinko_A song.json"
    before = (audio.read_bytes(), sidecar.read_bytes())
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    target = tmp_path / ("Producer_A song" + extension)
    assert migration._read_artist(target) == "Producer"
    assert not audio.exists() and not migration.journal_path(tmp_path).exists()
    if extension == ".mp3":
        raw = before[0]
        index = raw.index(b"TXXX")
        size = sum(
            value << shift
            for value, shift in zip(raw[index + 4 : index + 8], (21, 14, 7, 0), strict=True)
        )
        assert raw[index : index + 10 + size] in target.read_bytes()


@pytest.mark.parametrize(
    "area",
    [
        ".library/verified-sync-intents",
        ".library/download-intents",
        ".library/tag-intents",
        ".suno-staging/verified-sync/example",
    ],
)
def test_pending_publication_state_blocks_planning_without_mutation(tmp_path, area):
    recording(tmp_path, "drinko_A song")
    folder = tmp_path / area
    folder.mkdir(parents=True)
    pending = folder / "pending.part"
    pending.write_bytes(b"unreviewed state")
    before = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(migration.MigrationError, match="Pending"):
        migration.plan_migration(tmp_path, {ID1: "Producer"})
    assert before == {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }


def test_canonical_handle_is_reviewed_and_repaired_without_audio_rewrite(tmp_path):
    recording(tmp_path, "Producer_A song")
    source = tmp_path / "Producer_A song.m4a"
    before = source.read_bytes()
    handles = {ID1: "actual_creator"}
    assert (
        _apply_approved(
            tmp_path,
            {ID1: "Producer"},
            creator_handle_by_id=handles,
            reviewed_creator_handles=handles,
            writers_stopped=True,
        )
        == 1
    )
    data = json.loads((tmp_path / "Producer_A song.json").read_text())
    assert data["creator_handle"] == "actual_creator" and data["artist"] == "Producer"
    assert source.read_bytes() == before


def test_handle_mapping_cannot_bypass_independent_review(tmp_path):
    recording(tmp_path, "drinko_A song")
    with pytest.raises(migration.MigrationError, match="handle mapping review"):
        _apply_approved(
            tmp_path, {ID1: "Producer"}, creator_handle_by_id={ID1: "creator"}, writers_stopped=True
        )


@pytest.mark.parametrize("area", [".library/verified-sync-intents", ".suno-staging/example"])
def test_resume_blocks_new_pending_publications_without_changes(tmp_path, monkeypatch, area):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def stop(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migration, "_rename_no_replace", stop)
    with pytest.raises(RuntimeError):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    folder = tmp_path / area
    folder.mkdir(parents=True)
    (folder / "pending.part").write_bytes(b"pending")
    before = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(migration.MigrationError, match="Pending"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert before == {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }


def test_unreviewed_legacy_creator_handle_is_cleared(tmp_path):
    recording(tmp_path, "drinko_A song")
    side = tmp_path / "drinko_A song.json"
    data = json.loads(side.read_text())
    data["creator_handle"] = "wrong_profile"
    side.write_text(json.dumps(data))
    assert _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True) == 1
    new = json.loads((tmp_path / "Producer_A song.json").read_text())
    assert new["artist"] == "Producer" and new["creator_handle"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("creator_handle", "null"),
        ("artist", json.dumps("Producer")),
        ("id", json.dumps(ID1)),
        ("extra", '{"nested":NaN}'),
    ],
)
def test_resume_rejects_ambiguous_new_sidecar_content_before_mutation(
    tmp_path, monkeypatch, field, value
):
    recording(tmp_path, "drinko_A song")
    real = migration._rename_no_replace

    def stop(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migration, "_rename_no_replace", stop)
    with pytest.raises(RuntimeError):
        _apply_approved(tmp_path, {ID1: "Producer"}, writers_stopped=True)
    monkeypatch.setattr(migration, "_rename_no_replace", real)
    journal = migration.journal_path(tmp_path)
    plan = json.loads(journal.read_text())
    group = plan["groups"][0]
    content = group["new_content"].rstrip()
    group["new_content"] = content[:-1] + "," + json.dumps(field) + ":" + value + "}\n"
    group["new_hash"] = hashlib.sha256(group["new_content"].encode()).hexdigest()
    journal.write_text(json.dumps(plan))
    before = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(migration.MigrationError, match="producer decision"):
        _resume_approved(tmp_path, writers_stopped=True)
    assert before == {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
