"""Exercise the isolated in-memory artist patch model, never production."""

import hashlib
import shutil
import subprocess

import pytest

from research_checks.test_artist_allowlist_model import PROPS, _tag
from research_checks.test_artist_binary_proof_model import _frame, _packet_timing, _syncsafe
from tests.artist_binary_proof_model import Unsupported
from tests.id3_artist_patch_model import patch_id3_artist


def test_equal_length_patch_changes_only_requested_artist_bytes():
    before = _tag("Old")
    after = patch_id3_artist(before, "New", PROPS)
    assert len(before) == len(after)
    changed = [i for i, pair in enumerate(zip(before, after, strict=True)) if pair[0] != pair[1]]
    start = before.index(b"Old")
    assert changed == [start, start + 1, start + 2]
    assert after[start : start + 3] == b"New"
    assert patch_id3_artist(before, "Old", PROPS) == before


def test_equal_utf8_byte_length_not_character_count():
    before = _tag("\u00e9x")  # three UTF-8 bytes
    after = patch_id3_artist(before, "\u540d", PROPS)  # also three UTF-8 bytes
    assert len(after) == len(before)
    assert "\u540d".encode() in after
    with pytest.raises(Unsupported, match="byte length"):
        patch_id3_artist(before, "\u00e9", PROPS)


@pytest.mark.parametrize("artist", ["", "   ", "\u00a0\u00a0", "A\0B", "A\u202eB", "x" * 257])
def test_unsafe_artist_text_is_refused(artist):
    with pytest.raises(Unsupported):
        patch_id3_artist(_tag("Old"), artist, PROPS)


@pytest.mark.parametrize("frame_id", [b"TXXX", b"PRIV", b"SIGN", b"CHAP", b"CTOC", b"APIC"])
def test_private_unsupported_and_offset_bearing_frames_are_refused(frame_id):
    before = _tag("Old", _frame(frame_id, b"opaque"))
    with pytest.raises(Unsupported):
        patch_id3_artist(before, "New", PROPS)


def test_artist_frame_flags_or_conflicting_trailer_are_refused():
    before = bytearray(_tag("Old"))
    frame = before.index(b"TPE1")
    before[frame + 8] = 0x40
    with pytest.raises(Unsupported):
        patch_id3_artist(bytes(before), "New", PROPS)
    body = _frame(b"TPE1", b"\x03Old")
    appended = b"ID3\x04\x00\x10" + _syncsafe(len(body)) + body
    appended += b"3DI\x04\x00\x10" + _syncsafe(len(body))
    normal_audio = bytes.fromhex("fffb9064") + bytes(413)
    with pytest.raises(Unsupported):
        patch_id3_artist(_tag("Old", audio=normal_audio + appended), "New", PROPS)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools needed"
)
def test_real_mp3_packets_timing_and_decoded_audio_are_unchanged(tmp_path):
    from mutagen.id3 import ID3, TPE1

    from suno_dl.producer_migration import _audio_signature

    path = tmp_path / "before.mp3"
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
    after_path = tmp_path / "after.mp3"
    after_path.write_bytes(patch_id3_artist(before, "New", PROPS))
    assert _packet_timing(path) == _packet_timing(after_path)
    assert _audio_signature(path) == _audio_signature(after_path)
    assert ID3(after_path).get("TPE1").text == ["New"]


def test_opaque_mpeg_ancillary_semantics_are_not_proven():
    # A structurally framed MPEG payload could contain an application-defined
    # digest of the artist; this model cannot decode or understand it.
    embedded = b"digest:" + hashlib.sha256(b"Old").digest()
    frame = bytes.fromhex("fffb9064") + embedded + bytes(413 - len(embedded))
    before = _tag("Old", audio=frame)
    after = patch_id3_artist(before, "New", PROPS)
    assert embedded in after
    assert hashlib.sha256(b"New").digest() != hashlib.sha256(b"Old").digest()
