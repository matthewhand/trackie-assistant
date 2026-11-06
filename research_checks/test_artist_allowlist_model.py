"""Tests for deliberately narrow, non-production metadata allowlist research."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from research_checks.test_artist_binary_proof_model import _box, _frame, _syncsafe
from tests.artist_allowlist_model import scan_mp4_allowlist, verify_narrow_id3
from tests.artist_binary_proof_model import Properties, Unsupported

PROPS = Properties(1000, 1000, stat.S_IFREG | 0o600, 1, 0, ())


def _tag(artist, extra=b"", audio=None):
    if audio is None:
        # A synthetic MPEG-1 Layer III frame with structurally valid length.
        audio = bytes.fromhex("fffb9064") + bytes(413)
    body = _frame(b"TPE1", b"\x03" + artist.encode())
    body += _frame(b"TIT2", b"\x03Title") + extra + bytes(16)
    return b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body + audio


def _metadata(artist):
    return _box(b"\xa9ART", _box(b"data", b"\0\0\0\1\0\0\0\0" + artist.encode()))


def _mp4_tree(*extra):
    metadata = _box(b"udta", _box(b"meta", bytes(4) + _box(b"ilst", _metadata("Old"))))
    return (
        _box(b"ftyp", b"M4A ") + _box(b"mdat", b"audio") + _box(b"moov", metadata + b"".join(extra))
    )


def test_narrow_id3_accepts_standard_utf8_title_and_artist_only():
    verify_narrow_id3(_tag("Old"), _tag("New"), "New", PROPS, PROPS)


@pytest.mark.parametrize("frame_id", [b"TXXX", b"PRIV", b"SIGN", b"CHAP", b"CTOC", b"APIC"])
def test_narrow_id3_refuses_opaque_or_unsupported_frames(frame_id):
    extra = _frame(frame_id, b"opaque")
    with pytest.raises(Unsupported):
        verify_narrow_id3(_tag("Old", extra), _tag("New", extra), "New", PROPS, PROPS)


def test_narrow_id3_refuses_frame_flags_and_trailing_legacy_metadata():
    extra = _frame(b"TALB", b"\x03Album", flags=b"\x40\0")
    with pytest.raises(Unsupported):
        verify_narrow_id3(_tag("Old", extra), _tag("New", extra), "New", PROPS, PROPS)
    trailer = bytes.fromhex("fffb9064") + bytes(413) + b"TAG" + bytes(125)
    with pytest.raises(Unsupported):
        verify_narrow_id3(
            _tag("Old", audio=trailer), _tag("New", audio=trailer), "New", PROPS, PROPS
        )


def test_mp4_scan_refuses_private_boxes_at_top_and_nested_paths():
    # A synthetic no-track hierarchy only checks parser branches; not valid
    # audio, and no passing scan is evidence of complete MP4 semantics.
    with pytest.raises(Unsupported, match="required MP4 hierarchy"):
        scan_mp4_allowlist(_mp4_tree())
    with pytest.raises(Unsupported):
        scan_mp4_allowlist(_mp4_tree() + _box(b"uuid", bytes(24)))
    with pytest.raises(Unsupported):
        scan_mp4_allowlist(_mp4_tree(_box(b"trak", _box(b"uuid", bytes(24)))))


def test_real_mp4_refuses_unimplemented_sample_descriptions():
    path = Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a"
    data = bytearray(path.read_bytes())
    offset = data.index(b"mp4a")
    data[offset : offset + 4] = b"XXXX"
    with pytest.raises(Unsupported, match="type or size"):
        scan_mp4_allowlist(bytes(data))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools needed"
)
def test_real_mp3_with_simple_standard_frames_in_narrow_subset(tmp_path):
    from mutagen.id3 import ID3, TPE1

    path = tmp_path / "audio.mp3"
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
            str(path),
        ],
        check=True,
    )
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["Old"]))
    tags.save(path)
    before = path.read_bytes()
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["New"]))
    tags.save(path)
    verify_narrow_id3(before, path.read_bytes(), "New", PROPS, PROPS)


def test_id3_rejects_an_appended_second_tag_with_conflicting_artist():
    body = _frame(b"TPE1", b"\x03Old")
    appended = b"ID3\x04\x00\x10" + _syncsafe(len(body)) + body
    appended += b"3DI\x04\x00\x10" + _syncsafe(len(body))
    plain = bytes.fromhex("fffb9064") + bytes(413)
    verify_narrow_id3(_tag("Old", audio=plain), _tag("New", audio=plain), "New", PROPS, PROPS)
    with pytest.raises(Unsupported, match="metadata"):
        verify_narrow_id3(
            _tag("Old", audio=plain + appended),
            _tag("New", audio=plain + appended),
            "New",
            PROPS,
            PROPS,
        )


def test_mp4_scanner_inspects_real_single_track_fixture():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a").read_bytes()
    scan_mp4_allowlist(source)


def test_mp4_scanner_rejects_external_data_reference():
    from pathlib import Path

    data = bytearray((Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a").read_bytes())
    offset = data.index(b"url ")
    data[offset + 7] = 0
    with pytest.raises(Unsupported, match="external"):
        scan_mp4_allowlist(bytes(data))


def test_mp4_scanner_rejects_private_atom_even_when_well_formed():
    from pathlib import Path

    data = (Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a").read_bytes()
    with pytest.raises(Unsupported, match="opaque"):
        scan_mp4_allowlist(data + _box(b"uuid", b"private"))


def test_mp4_scanner_rejects_incomplete_hierarchy():
    with pytest.raises(Unsupported, match="required MP4 hierarchy"):
        scan_mp4_allowlist(_box(b"free", b""))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_narrow_id3_accepts_multiminute_real_mpeg_fixture(tmp_path):
    from mutagen.id3 import ID3, TPE1

    from tests.artist_allowlist_model import _require_plain_mpeg_frames
    from tests.artist_binary_proof_model import _id3_structure

    path = tmp_path / "long.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=110",
            "-c:a",
            "libmp3lame",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["Old"]))
    tags.save(path)
    before = path.read_bytes()
    _, media, _, _ = _id3_structure(before)
    assert len(media) > 0
    assert _require_plain_mpeg_frames(media) > 4096
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["New"]))
    tags.save(path)
    verify_narrow_id3(before, path.read_bytes(), "New", PROPS, PROPS)


def test_mpeg_frame_budget_is_independently_bounded():
    from tests.artist_allowlist_model import MAX_MPEG_FRAMES, _require_plain_mpeg_frames

    frame = bytes.fromhex("fffb9064") + bytes(413)
    assert _require_plain_mpeg_frames(frame * MAX_MPEG_FRAMES) == MAX_MPEG_FRAMES
    with pytest.raises(Unsupported, match="frame bounds"):
        _require_plain_mpeg_frames(frame * (MAX_MPEG_FRAMES + 1))
