"""Embedded tags: artist/title written on sync (#69) and ``feat.`` survival (#70).

The fixture is a 1.4 KB, 0.5 s AAC ``.m4a`` generated once with
``ffmpeg -f lavfi -i sine=frequency=440:duration=0.5 -c:a aac -b:a 8k -ac 1``
— a real container (so both backends accept it) that is small enough to copy
around in every test, and above the downloader's 1 KB minimum payload size.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from suno_dl.tagging import (
    TagResult,
    _write_with_mutagen,
    read_tags,
    retag_tree,
    tag_file,
    tags_from_stem,
    untagged_files,
    write_lyrics_sidecar,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tiny.m4a"

pytest.importorskip("mutagen", reason="tag write assertions need mutagen")


@pytest.fixture
def exercise_quarantined_tagging_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise disabled staging internals, not the production safety guard."""
    from suno_dl import tagging

    monkeypatch.setattr(tagging, "_embedded_tag_write_guard", lambda: None)


def _tiny(tmp_path: Path, name: str = "song.m4a") -> Path:
    """A private copy of the fixture — tests must never mutate the fixture."""
    dest = tmp_path / name
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def test_fixture_is_a_real_m4a() -> None:
    """Guard: a truncated or non-media fixture would make every test vacuous."""
    from mutagen import File

    assert FIXTURE.stat().st_size > 1024  # the downloader's minimum payload
    audio = File(str(FIXTURE), easy=True)
    assert audio is not None and audio.info.length > 0


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_writes_artist_and_title(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    result = tag_file(path, artist="drinko", title="Absolutely Certain")
    assert result.tagged is True
    assert result.backend == "mutagen"
    tags = read_tags(path)
    assert tags["artist"] == "drinko"
    assert tags["title"] == "Absolutely Certain"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_never_reencodes_audio(tmp_path: Path) -> None:
    """Tagging is metadata-only: the AAC payload must be byte-identical."""
    path = _tiny(tmp_path)
    before = path.read_bytes()
    tag_file(path, artist="drinko", title="Absolutely Certain")
    after = path.read_bytes()

    def mdat(payload: bytes) -> bytes:
        start = payload.find(b"mdat")
        size = int.from_bytes(payload[start - 4 : start], "big")  # size includes the header
        return payload[start + 4 : start - 4 + size]

    assert mdat(after) == mdat(before)


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_overwrites_previous_tags(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    tag_file(path, artist="drinko", title="First Title")
    tag_file(path, artist="drinko", title="Second Title")
    assert read_tags(path)["title"] == "Second Title"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_empty_values_are_refused(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    before = path.read_bytes()
    for artist, title in (("", "T"), ("A", ""), ("", "")):
        result = tag_file(path, artist=artist, title=title)
        assert isinstance(result, TagResult)
        assert result.tagged is False
    assert path.read_bytes() == before  # untouched


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_missing_file_is_not_fatal(tmp_path: Path) -> None:
    result = tag_file(tmp_path / "nope.m4a", artist="drinko", title="T")
    assert result.tagged is False
    assert "no such file" in result.reason


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_survives_corrupt_bytes(tmp_path: Path) -> None:
    """A file mutagen cannot parse is never a hard error — the audio stands."""
    path = tmp_path / "broken.m4a"
    path.write_bytes(b"not a media container at all" * 64)
    before = path.read_bytes()
    result = tag_file(path, artist="drinko", title="T")
    assert result.tagged is False
    assert path.read_bytes() == before


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_failure_leaves_the_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing remux must not leave a .tag artefact or a truncated file."""
    monkeypatch.setattr("suno_dl.tagging._write_with_mutagen", lambda *a: (False, "absent"))
    monkeypatch.setattr("suno_dl.tagging.find_ffmpeg", lambda: "/nonexistent/ffmpeg")
    path = _tiny(tmp_path)
    before = path.read_bytes()
    result = tag_file(path, artist="drinko", title="T")
    assert result.tagged is False
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.m4a"]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_wavs_use_id3_frames(tmp_path: Path) -> None:
    """WAV has no easy key/value mapping; the ID3 path must still work."""
    import shutil as _sh

    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg needed to synthesise a wav fixture")
    path = tmp_path / "song.wav"
    subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", str(path), "-y"],
        check=True,
        capture_output=True,
    )
    assert tag_file(path, artist="drinko", title="Song (feat. Alice)").tagged is True
    from mutagen.wave import WAVE

    frames = {k: str(v) for k, v in (WAVE(path).tags or {}).items()}
    assert frames["TPE1"] == "drinko"
    assert frames["TIT2"] == "Song (feat. Alice)"


def _raise(msg: str):
    def boom(*_a, **_kw):
        raise ValueError(msg)

    return boom


def test_read_tags_of_untagged_and_broken_files(tmp_path: Path) -> None:
    assert read_tags(_tiny(tmp_path, "a.m4a")) == {}
    broken = tmp_path / "b.m4a"
    broken.write_bytes(b"junk" * 200)
    assert read_tags(broken) == {}


def test_read_tags_without_mutagen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mutagen", None)  # None => ImportError
    assert read_tags(_tiny(tmp_path)) == {}


def test_mutagen_absent_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "mutagen", None)
    path = _tiny(tmp_path)
    ok, reason = _write_with_mutagen(path, "drinko", "T")
    assert ok is False
    assert "mutagen" in reason


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_without_any_backend_is_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("suno_dl.tagging._write_with_mutagen", lambda *a: (False, "absent"))
    monkeypatch.setattr("suno_dl.tagging._write_with_ffmpeg", lambda *a: (False, "absent"))
    path = _tiny(tmp_path)
    before = path.read_bytes()
    result = tag_file(path, artist="drinko", title="T")
    assert result.tagged is False
    assert "absent" in result.reason
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.m4a"]  # no .tag debris


# --- #70: a title containing "feat." must round-trip in full -----------------


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_feat_title_round_trips_in_tag(tmp_path: Path) -> None:
    title = "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)"
    path = _tiny(tmp_path, f"drinko_{title}.m4a")
    result = tag_file(path, artist="drinko", title=title)
    assert result.tagged is True
    assert read_tags(path)["title"] == title  # full string, nothing after "feat"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_feat_filename_is_ma_safe_and_tag_is_primary(tmp_path: Path) -> None:
    """The filename must not hand a tag-guessing player a fake extension."""
    from suno_dl.naming import build_filename

    title = "Song (feat. Alice) - Pt. 2"
    name = build_filename("{handle}_{title}", title=title, handle="drinko", clip_id="abc")
    assert name == "drinko_Song (feat_ Alice) - Pt_ 2"
    assert "." not in name  # only the downloader's extension may contain a dot

    path = _tiny(tmp_path, f"{name}.m4a")
    tag_file(path, artist="drinko", title=title)
    assert read_tags(path)["title"] == title


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_remux_by_ffmpeg_preserves_tags(tmp_path: Path) -> None:
    """--extract-audio (mp4 -> m4a, -c copy) carries the tags across."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    src = tmp_path / "clip.mp4"
    src.write_bytes(FIXTURE.read_bytes())
    tag_file(src, artist="drinko", title="Song (feat. Alice)")
    from suno_dl.downloader import extract_audio_files

    written = extract_audio_files([src])
    assert written == [tmp_path / "clip.m4a"]
    assert read_tags(written[0])["title"] == "Song (feat. Alice)"


# --- retag: one-shot migration of an archive that predates tags --------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        (
            "drinko_Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)",
            ("drinko", "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)"),
        ),
        ("drinko_Song (feat_ Alice)", ("drinko", "Song (feat_ Alice)")),
        ("untitled", ("", "untitled")),
        ("drinko_", ("", "drinko_")),
    ],
)
def test_tags_from_stem(stem: str, expected: tuple[str, str]) -> None:
    assert tags_from_stem(stem) == expected


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_tags_untagged_files(tmp_path: Path) -> None:
    a = _tiny(tmp_path, "drinko_Ajani, Nacatl Pariah (feat. Pyotr Kropotkin).m4a")
    b = _tiny(tmp_path, "drinko_Already Tagged.m4a")
    tag_file(b, artist="drinko", title="Already Tagged")
    (tmp_path / "notes.txt").write_text("not audio")
    (tmp_path / "drinko_Art.cover.jpeg").write_bytes(b"\xff\xd8\xff")

    stats = retag_tree(tmp_path)
    assert stats == {"scanned": 2, "tagged": 1, "skipped": 1, "failed": 0}
    assert read_tags(a)["title"] == "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)"
    assert read_tags(a)["artist"] == "drinko"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_is_recursive_and_ignores_covers(tmp_path: Path) -> None:
    nested = tmp_path / "drinko" / "more"
    nested.mkdir(parents=True)
    a = _tiny(nested, "drinko_Deep Cut.m4a")
    (nested / "drinko_Deep Cut.cover.jpeg").write_bytes(b"\xff\xd8\xff")
    stats = retag_tree(tmp_path)
    assert stats["scanned"] == 1
    assert read_tags(a)["title"] == "Deep Cut"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_artist_override(tmp_path: Path) -> None:
    """--artist replaces the handle the filename implies; the title is unchanged."""
    a = _tiny(tmp_path, "Some_Old-Pattern_Title Here.m4a")
    retag_tree(tmp_path, artist="drinko")
    assert read_tags(a)["artist"] == "drinko"
    assert read_tags(a)["title"] == "Old-Pattern_Title Here"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_dry_run_writes_nothing(tmp_path: Path) -> None:
    a = _tiny(tmp_path, "drinko_Title.m4a")
    before = a.read_bytes()
    stats = retag_tree(tmp_path, dry_run=True)
    assert stats["tagged"] == 1
    assert a.read_bytes() == before
    assert read_tags(a) == {}


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_reports_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tiny(tmp_path, "drinko_Title.m4a")
    monkeypatch.setattr(
        "suno_dl.tagging.tag_file", lambda *a, **kw: TagResult(False, "none", "", "", "boom")
    )
    assert retag_tree(tmp_path)["failed"] == 1


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_untagged_files_lists_only_untagged_audio(tmp_path: Path) -> None:
    a = _tiny(tmp_path, "drinko_One.m4a")
    b = _tiny(tmp_path, "drinko_Two.m4a")
    tag_file(b, artist="drinko", title="Two")
    (tmp_path / "notes.txt").write_text("not audio")
    (tmp_path / "drinko_Three.cover.jpeg").write_bytes(b"\xff\xd8\xff")
    assert untagged_files(tmp_path) == [a]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_never_reencodes(tmp_path: Path) -> None:
    """The migration must not touch the audio payload of a 171-file archive."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg executable is not installed")
    path = _tiny(tmp_path, "drinko_Ajani, Nacatl Pariah (feat. Pyotr Kropotkin).m4a")
    before = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    retag_tree(tmp_path)
    after = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if before.returncode != 0:
        pytest.skip("ffmpeg cannot decode the fixture")
    assert after.stdout == before.stdout


# --- Lyrics support (#75) -----------------------------------------------------


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_writes_lyrics_to_mp4(tmp_path: Path) -> None:
    """Lyrics are written to the ©lyr atom in MP4/M4A files."""
    path = _tiny(tmp_path)
    lyrics = "Line 1\nLine 2\nLine 3"
    result = tag_file(path, artist="drinko", title="Test Song", lyrics=lyrics)
    assert result.tagged is True
    assert result.lyrics == lyrics

    # Read back using mutagen MP4 directly
    from mutagen.mp4 import MP4

    mp4 = MP4(path)
    assert mp4.get("\xa9lyr") == [lyrics]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tag_file_skips_empty_lyrics(tmp_path: Path) -> None:
    """Empty or whitespace-only lyrics are not written."""
    path = _tiny(tmp_path)
    for lyrics in ("", "   ", "\n\t"):
        result = tag_file(path, artist="drinko", title="Test Song", lyrics=lyrics)
        assert result.tagged is True
        assert result.lyrics is None or result.lyrics == lyrics
        # The tag should not have lyrics
        from mutagen.mp4 import MP4

        mp4 = MP4(path)
        assert "\xa9lyr" not in mp4


def test_write_lyrics_sidecar_lrc(tmp_path: Path) -> None:
    """write_lyrics_sidecar creates .lrc file with lyrics content."""
    path = _tiny(tmp_path, "song.m4a")
    lyrics = "[00:00.00]Line 1\n[00:10.00]Line 2"
    sidecar = write_lyrics_sidecar(path, lyrics, "lrc")
    assert sidecar is not None
    assert sidecar.suffix == ".lrc"
    assert sidecar.read_text(encoding="utf-8") == lyrics


def test_write_lyrics_sidecar_txt(tmp_path: Path) -> None:
    """write_lyrics_sidecar creates .txt file with lyrics content."""
    path = _tiny(tmp_path, "song.m4a")
    lyrics = "Line 1\nLine 2"
    sidecar = write_lyrics_sidecar(path, lyrics, "txt")
    assert sidecar is not None
    assert sidecar.suffix == ".txt"
    assert sidecar.read_text(encoding="utf-8") == lyrics


def test_write_lyrics_sidecar_skips_empty(tmp_path: Path) -> None:
    """write_lyrics_sidecar returns None for empty lyrics."""
    path = _tiny(tmp_path, "song.m4a")
    assert write_lyrics_sidecar(path, "", "lrc") is None
    assert write_lyrics_sidecar(path, "   ", "lrc") is None
    assert write_lyrics_sidecar(path, "\n\t", "lrc") is None


def test_write_lyrics_sidecar_invalid_format(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """write_lyrics_sidecar logs warning and returns None for unknown format."""
    path = _tiny(tmp_path, "song.m4a")
    assert write_lyrics_sidecar(path, "lyrics", "invalid") is None
    assert "Unknown lyrics sidecar format" in caplog.text


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_writes_lyrics_sidecar_for_tagged_files(tmp_path: Path) -> None:
    """retag_tree with lyrics_sidecar writes sidecar for files with lyrics in tags."""
    path = _tiny(tmp_path, "drinko_Test Song.m4a")
    # Tag with lyrics
    tag_file(path, artist="drinko", title="Test Song", lyrics="Line 1\nLine 2")
    # Run retag with lyrics_sidecar
    stats = retag_tree(tmp_path, lyrics_sidecar="lrc")
    assert stats["skipped"] == 1  # file already had title tag
    # Sidecar should be created
    sidecar = path.with_suffix(".lrc")
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8") == "Line 1\nLine 2"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_tree_lyrics_sidecar_skips_files_without_lyrics(tmp_path: Path) -> None:
    """retag_tree with lyrics_sidecar does not create sidecar for files without lyrics."""
    path = _tiny(tmp_path, "drinko_Test Song.m4a")
    # Tag without lyrics
    tag_file(path, artist="drinko", title="Test Song")
    stats = retag_tree(tmp_path, lyrics_sidecar="lrc")
    assert stats["skipped"] == 1
    sidecar = path.with_suffix(".lrc")
    assert not sidecar.exists()


def test_lyrics_sidecar_never_overwrites_unowned_file(tmp_path: Path) -> None:
    audio = _tiny(tmp_path, "song.m4a")
    lyrics = tmp_path / "song.lrc"
    lyrics.write_text("unrelated", encoding="utf-8")
    assert write_lyrics_sidecar(audio, "new lyrics", "lrc") is None
    assert lyrics.read_text(encoding="utf-8") == "unrelated"
    assert not list(tmp_path.glob(".suno-lyrics-*.part"))


def test_lyrics_sidecar_never_follows_symlink(tmp_path: Path) -> None:
    audio = _tiny(tmp_path, "song.m4a")
    outside = tmp_path / "other.txt"
    outside.write_text("unrelated", encoding="utf-8")
    link = tmp_path / "song.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert write_lyrics_sidecar(audio, "new lyrics", "txt") is None
    assert outside.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_dry_run_never_creates_lyrics_for_already_tagged_track(
    tmp_path: Path, monkeypatch
) -> None:
    import suno_dl.tagging as tagging

    audio = tmp_path / "song.m4a"
    audio.write_bytes(b"placeholder")
    monkeypatch.setattr(tagging, "read_tags", lambda path: {"title": "Song", "lyrics": "words"})
    stats = tagging.retag_tree(tmp_path, dry_run=True, lyrics_sidecar="lrc")
    assert stats["skipped"] == 1
    assert not (tmp_path / "song.lrc").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_refuses_outside_symlink_before_changing_any_file(
    tmp_path: Path, monkeypatch
) -> None:
    import suno_dl.tagging as tagging

    root = tmp_path / "archive"
    root.mkdir()
    valid = root / "artist_a.m4a"
    valid.write_bytes(b"inside")
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"outside")
    link = root / "artist_z.m4a"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    touched = []
    monkeypatch.setattr(tagging, "tag_file", lambda *args, **kwargs: touched.append(args))
    with pytest.raises(RuntimeError, match="Unsafe linked audio"):
        tagging.retag_tree(root)
    assert not touched
    assert valid.read_bytes() == b"inside"
    assert outside.read_bytes() == b"outside"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_retag_refuses_hardlinked_audio_before_changing_any_file(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    import suno_dl.tagging as tagging

    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"outside")
    try:
        os.link(outside, root / "artist_song.m4a")
    except OSError:
        pytest.skip("hardlinks unavailable")
    touched = []
    monkeypatch.setattr(tagging, "tag_file", lambda *args, **kwargs: touched.append(args))
    with pytest.raises(RuntimeError, match="Unsafe linked audio"):
        tagging.retag_tree(root)
    assert not touched and outside.read_bytes() == b"outside"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_private_stage_ignores_planted_legacy_symlink_and_preserves_mode(
    tmp_path: Path,
) -> None:
    """A guessed old temp name cannot redirect ffmpeg or replace the recording."""
    from suno_dl.tagging import _write_with_ffmpeg

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path, "Artist_Song.m4a")
    os.chmod(path, 0o640)
    before = path.stat()
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"outside sentinel")
    planted = tmp_path / ".Artist_Song.tag.m4a"
    try:
        planted.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    ok, detail = _write_with_ffmpeg(path, "Artist", "New title")

    assert ok, detail
    assert outside.read_bytes() == b"outside sentinel"
    assert planted.is_symlink()
    assert not path.is_symlink()
    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert read_tags(path)["title"] == "New title"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_linked_source_without_changes(tmp_path: Path, kind: str) -> None:
    from suno_dl.tagging import _write_with_ffmpeg

    original = _tiny(tmp_path, "original.m4a")
    before = original.read_bytes()
    target = tmp_path / "linked.m4a"
    try:
        if kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
    except OSError:
        pytest.skip(f"{kind} unavailable")

    ok, _detail = _write_with_ffmpeg(target, "Artist", "Title")

    assert not ok
    assert original.read_bytes() == before
    assert target.exists()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_destination_swapped_during_remux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path, "Artist_Song.m4a")
    original_bytes = path.read_bytes()
    displaced = tmp_path / "displaced.m4a"
    real_run = tagging.subprocess.run

    def swap_after_remux(*args, **kwargs):
        result = real_run(*args, **kwargs)
        if not displaced.exists():
            path.rename(displaced)
            path.write_bytes(b"competing recording")
        return result

    monkeypatch.setattr(tagging.subprocess, "run", swap_after_remux)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "changed" in detail
    assert displaced.read_bytes() == original_bytes
    assert path.read_bytes() == b"competing recording"
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_planted_stage_output_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    original_bytes = path.read_bytes()
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"outside sentinel")
    real_run = tagging.subprocess.run

    def plant_before_remux(cmd, **kwargs):
        target = Path(cmd[-1])
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(tagging.subprocess, "run", plant_before_remux)
    ok, _detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok
    assert path.read_bytes() == original_bytes
    assert outside.read_bytes() == b"outside sentinel"
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_preserves_source_with_extended_attributes(tmp_path: Path) -> None:
    from suno_dl.tagging import _write_with_ffmpeg

    if not shutil.which("ffmpeg") or not hasattr(os, "setxattr"):
        pytest.skip("ffmpeg or extended attributes unavailable")
    path = _tiny(tmp_path)
    before = path.read_bytes()
    try:
        os.setxattr(path, "user.trackie-test", b"keep this")
    except OSError:
        pytest.skip("filesystem does not support extended attributes")

    ok, detail = _write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "extended attributes" in detail
    assert path.read_bytes() == before
    assert os.getxattr(path, "user.trackie-test") == b"keep this"
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="extended attributes unavailable")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_when_extended_attributes_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    before = path.read_bytes()

    def fail(_fd):
        raise OSError("xattr verification failed")

    monkeypatch.setattr(tagging.os, "listxattr", fail)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "xattr verification failed" in detail
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_preserves_two_audio_streams(tmp_path: Path) -> None:
    import json

    from suno_dl.tagging import _write_with_ffmpeg

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    path = tmp_path / "two-streams.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.2",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "aac",
            "-metadata",
            "comment=preserve this",
            str(path),
        ],
        check=True,
        capture_output=True,
    )

    def streams():
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        return [
            item for item in json.loads(result.stdout)["streams"] if item["codec_type"] == "audio"
        ]

    assert len(streams()) == 2
    ok, detail = _write_with_ffmpeg(path, "Artist", "Two streams")
    assert ok, detail
    assert len(streams()) == 2
    assert read_tags(path)["title"] == "Two streams"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_preserves_or_refuses_itunes_freeform_tag(tmp_path: Path) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm

    from suno_dl.tagging import _write_with_ffmpeg

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    path = _tiny(tmp_path)
    media = MP4(path)
    key = "----:com.apple.iTunes:trackie-custom"
    media[key] = [MP4FreeForm(b"must survive")]
    media.save()
    before = path.read_bytes()

    ok, _detail = _write_with_ffmpeg(path, "Artist", "Title")

    if ok:
        assert MP4(path)[key][0] == b"must survive"
    else:
        assert path.read_bytes() == before
        assert MP4(path)[key][0] == b"must survive"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_if_metadata_inspector_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(tagging, "find_ffmpeg", lambda: ffmpeg)
    original_which = shutil.which
    monkeypatch.setattr(
        tagging.shutil, "which", lambda name: None if name == "ffprobe" else original_which(name)
    )

    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "ffprobe" in detail
    assert path.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_abrupt_exit_leaves_no_scannable_staged_media(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path, "Artist_Song.m4a")
    before = path.read_bytes()
    code = """
import os, sys
from pathlib import Path
from suno_dl import tagging
tagging._embedded_tag_write_guard = lambda: None
tagging.subprocess.run = lambda *args, **kwargs: os._exit(91)
tagging._write_with_ffmpeg(Path(sys.argv[1]), 'Artist', 'Title')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 91
    assert path.read_bytes() == before
    stages = list(tmp_path.glob(".suno-tag-*"))
    assert len(stages) == 1
    assert list(stages[0].iterdir())
    assert all(item.suffix == ".part" for item in stages[0].iterdir())
    assert sorted(tmp_path.rglob("*.m4a")) == [path]


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_if_directory_cannot_be_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    before = path.read_bytes()

    def fail(_fd):
        raise OSError("directory sync unsupported")

    monkeypatch.setattr(tagging.os, "fsync", fail)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")
    assert not ok and "directory sync unsupported" in detail
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_empty_lyrics_never_discards_existing_lyrics(tmp_path: Path) -> None:
    from mutagen.mp4 import MP4

    from suno_dl.tagging import _write_with_ffmpeg

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    path = _tiny(tmp_path)
    tags = MP4(path)
    tags["\xa9lyr"] = ["original lyrics"]
    tags.save()
    before = path.read_bytes()

    ok, _detail = _write_with_ffmpeg(path, "Artist", "Title", lyrics="   ")

    if ok:
        assert MP4(path)["\xa9lyr"] == ["original lyrics"]
    else:
        assert path.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_when_native_metadata_inspector_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise ImportError("Mutagen missing")

    monkeypatch.setattr(tagging, "_ffmpeg_integrity_evidence", fail)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "Mutagen missing" in detail
    assert path.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_reports_directory_sync_failure_after_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from suno_dl import tagging

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    path = _tiny(tmp_path)
    real_replace = tagging.os.replace
    real_fsync = tagging.os.fsync
    replaced = False

    def replace_and_mark(source, destination):
        nonlocal replaced
        real_replace(source, destination)
        replaced = True

    def fail_after_replace(fd):
        if replaced:
            raise OSError("post-replace sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(tagging.os, "replace", replace_and_mark)
    monkeypatch.setattr(tagging.os, "fsync", fail_after_replace)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert ok, detail
    assert "replacement could not be synced" in caplog.text
    assert read_tags(path)["title"] == "Title"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="cannot verify source xattrs")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_ffmpeg_refuses_actual_missing_mutagen_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from suno_dl import tagging

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")
    path = _tiny(tmp_path)
    before = path.read_bytes()
    original_import = builtins.__import__
    calls = []

    def without_mutagen(name, *args, **kwargs):
        if name == "mutagen":
            raise ModuleNotFoundError("simulated unavailable Mutagen")
        return original_import(name, *args, **kwargs)

    def no_subprocess(*args, **kwargs):
        calls.append(args)
        raise AssertionError("ffmpeg must not run without Mutagen")

    monkeypatch.setattr(tagging, "find_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(builtins, "__import__", without_mutagen)
    monkeypatch.setattr(tagging.subprocess, "run", no_subprocess)
    ok, detail = tagging._write_with_ffmpeg(path, "Artist", "Title")

    assert not ok and "Mutagen" in detail
    assert calls == []
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_failure_never_invokes_ffmpeg_or_changes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()

    def partial_then_fail(output, *_args):
        output.write_bytes(b"partial staged edit")
        raise ValueError("interrupted tag write")

    def no_ffmpeg(*_args):
        pytest.fail("experimental ffmpeg fallback was invoked")

    monkeypatch.setattr(tagging, "_write_with_mutagen", partial_then_fail)
    monkeypatch.setattr(tagging, "_write_with_ffmpeg", no_ffmpeg)
    result = tag_file(path, artist="Artist", title="Title", lyrics="words")
    assert not result.tagged
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_rejects_source_path_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"foreign file")
    original_writer = tagging._write_with_mutagen

    def swap_after_write(output, *args):
        result = original_writer(output, *args)
        os.replace(replacement, path)
        return result

    monkeypatch.setattr(tagging, "_write_with_mutagen", swap_after_write)
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == b"foreign file"


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="xattrs unavailable")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_refuses_source_xattr_without_changing_it(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    before = path.read_bytes()
    try:
        os.setxattr(path, b"user.suno_test", b"keep")
    except OSError:
        pytest.skip("filesystem does not support user xattrs")
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == before
    assert os.getxattr(path, b"user.suno_test") == b"keep"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_rejects_unverifiable_inode_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import producer_migration

    path = _tiny(tmp_path)
    before = path.read_bytes()

    def fail(_path):
        raise producer_migration.MigrationError("unverifiable inode flags")

    monkeypatch.setattr(producer_migration, "_default_inode_flags", fail)
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == before


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_interrupted_process_leaves_original_and_no_media_staging(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    before = path.read_bytes()
    code = """
import os
import sys
from pathlib import Path
from suno_dl import tagging
def die(output, *args):
    output.write_bytes(b'partial edit')
    os._exit(37)
tagging._embedded_tag_write_guard = lambda: None
tagging._write_with_mutagen = die
tagging.tag_file(Path(sys.argv[1]), artist='Artist', title='Title')
"""
    process = subprocess.run([sys.executable, "-c", code, str(path)], check=False)
    assert process.returncode == 37
    assert path.read_bytes() == before
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and p != path]
    assert leftovers
    assert all(p.suffix not in {".m4a", ".mp3", ".wav", ".flac", ".ogg"} for p in leftovers)


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_preserves_mode_on_success(tmp_path: Path) -> None:
    path = _tiny(tmp_path)
    path.chmod(0o640)
    result = tag_file(path, artist="Artist", title="Title")
    assert result.tagged
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_refuses_unsafe_staged_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"untouched")

    def plant(output, *_args):
        output.unlink()
        output.symlink_to(sentinel)
        return True, "mutagen"

    monkeypatch.setattr(tagging, "_write_with_mutagen", plant)
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == before
    assert sentinel.read_bytes() == b"untouched"


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_fsync_failure_before_replace_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()
    fsync = os.fsync
    calls = 0

    def failing_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staged file fsync failure")
        return fsync(fd)

    monkeypatch.setattr(tagging.os, "fsync", failing_fsync)
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == before


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_mutagen_refuses_nodump_inode_flag(tmp_path: Path) -> None:
    if not shutil.which("chattr"):
        pytest.skip("chattr unavailable")
    path = _tiny(tmp_path)
    before = path.read_bytes()
    setting = subprocess.run(["chattr", "+d", str(path)], capture_output=True, check=False)
    if setting.returncode:
        pytest.skip("filesystem does not support nodump")
    try:
        result = tag_file(path, artist="Artist", title="Title")
        assert not result.tagged
        assert path.read_bytes() == before
    finally:
        subprocess.run(["chattr", "-d", str(path)], capture_output=True, check=False)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux allocation checks")
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_staged_tag_rejects_sparse_source_with_preallocation_past_eof(tmp_path: Path) -> None:
    import ctypes

    path = _tiny(tmp_path)
    with path.open("r+b") as stream:
        stream.seek(8 * 1024 * 1024 - 1)
        stream.write(b"\0")
        stream.flush()
        libc = ctypes.CDLL(None, use_errno=True)
        fallocate = libc.fallocate
        fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        fallocate.restype = ctypes.c_int
        if fallocate(stream.fileno(), 1, 16 * 1024 * 1024, 16 * 1024 * 1024) != 0:
            pytest.skip("filesystem does not support keep-size preallocation")
    before = path.stat()
    if before.st_blocks * 512 < before.st_size:
        pytest.skip("filesystem did not account preallocation in st_blocks")
    with path.open("rb") as stream:
        if os.lseek(stream.fileno(), 0, os.SEEK_HOLE) >= before.st_size:
            pytest.skip("filesystem does not expose holes")
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert "sparse" in result.reason
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_blocks) == (
        before.st_ino,
        before.st_size,
        before.st_blocks,
    )
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_staged_tag_refuses_unavailable_allocation_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()
    original_lseek = os.lseek

    def unsupported(fd, pos, whence):
        if whence in (os.SEEK_DATA, os.SEEK_HOLE):
            raise OSError("allocation query unsupported")
        return original_lseek(fd, pos, whence)

    monkeypatch.setattr(tagging.os, "lseek", unsupported)
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_staged_tag_refuses_insufficient_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(tagging.shutil, "disk_usage", lambda _path: usage_type(0, 0, 0))
    result = tag_file(path, artist="Artist", title="Title")
    assert not result.tagged
    assert "insufficient staging space" in result.reason
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("caller", ["direct", "retag", "download"])
@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_post_replace_fsync_failure_stops_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    from suno_dl import tagging
    from suno_dl.downloader import _write_tags
    from suno_dl.models import TrackInfo

    path = _tiny(tmp_path, "Artist_Title.m4a")
    original_fsync = os.fsync
    calls = 0

    def failing_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("directory fsync failed after replace")
        return original_fsync(fd)

    monkeypatch.setattr(tagging.os, "fsync", failing_fsync)
    stats: dict[str, int] = {}
    with pytest.raises(tagging.TagPublicationUncertain, match="inspect the file before retrying"):
        if caller == "direct":
            tag_file(path, artist="Artist", title="Title")
        elif caller == "retag":
            retag_tree(tmp_path, artist="Artist")
        else:
            track = TrackInfo(
                id="test", title="Title", artist="Artist", page_url="https://example.com"
            )
            _write_tags(path, track, "source", stats)
    assert read_tags(path)["title"] == "Title"
    assert not stats.get("tagged")


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_post_replace_close_failure_attempts_all_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    replace = os.replace
    close = os.close
    published = False
    closed = []

    def observing_replace(src, dst):
        nonlocal published
        replace(src, dst)
        published = True

    def failing_close(fd):
        if published:
            closed.append(fd)
            close(fd)
            if len(closed) == 1:
                raise OSError("injected close error")
            return
        close(fd)

    monkeypatch.setattr(tagging.os, "replace", observing_replace)
    monkeypatch.setattr(tagging.os, "close", failing_close)
    with pytest.raises(tagging.TagPublicationUncertain, match="cleanup failed"):
        tag_file(path, artist="Artist", title="Title")
    assert len(closed) >= 3
    assert read_tags(path)["title"] == "Title"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_metadata_workflow_stops_before_sidecar_on_uncertain_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from suno_dl import metadata, tagging

    path = _tiny(tmp_path)
    sidecar = tmp_path / "song.json"
    sidecar.write_text('{"artist":"old","title":"old"}')
    before = sidecar.read_bytes()
    entry = SimpleNamespace(sidecar=sidecar, base=tmp_path / "song")
    plan = metadata.MetadataPlan(
        "track-id",
        entry,
        {"artist": ("old", "Artist")},
        {"artist": "Artist", "title": "Title"},
        (path,),
    )
    monkeypatch.setattr(metadata, "read_sidecar", lambda _path: {"artist": "old", "title": "old"})
    original_fsync = os.fsync
    calls = 0

    def failing_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("directory fsync failed after replace")
        return original_fsync(fd)

    monkeypatch.setattr(tagging.os, "fsync", failing_fsync)
    stats: dict[str, int] = {}
    with pytest.raises(tagging.TagPublicationUncertain):
        metadata.apply_metadata(plan, dry_run=False, sidecar=True, lyrics_sidecar=None, stats=stats)
    assert sidecar.read_bytes() == before
    assert not stats.get("tagged")
    assert not stats.get("metadata_updated")


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_replace_error_is_reported_as_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import tagging

    path = _tiny(tmp_path)
    before = path.read_bytes()

    def failed_replace(*_args):
        raise OSError("rename outcome unavailable")

    monkeypatch.setattr(tagging.os, "replace", failed_replace)
    with pytest.raises(tagging.TagPublicationUncertain, match="inspect the file before retrying"):
        tag_file(path, artist="Artist", title="Title")
    assert path.read_bytes() == before


@pytest.mark.usefixtures("exercise_quarantined_tagging_writer")
def test_tagged_wav_is_read_and_retag_is_idempotent(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "Unverified Filename_Song.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x01" * 800)
    assert tag_file(path, artist="Verified Artist", title="Actual Title").tagged
    assert read_tags(path)["artist"] == "Verified Artist"
    assert read_tags(path)["title"] == "Actual Title"
    before = path.read_bytes()
    stats = retag_tree(tmp_path, artist="Different Operator Artist")
    assert stats["skipped"] == 1 and stats["tagged"] == 0
    assert path.read_bytes() == before
