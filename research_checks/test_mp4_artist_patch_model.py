"""Offline research for fixed-width in-memory MP4 artist byte patching."""

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from research_checks.test_artist_binary_proof_model import PROPS, _box, _mp4, _packet_timing
from tests.artist_binary_proof_model import Unsupported, _mp4_structure
from tests.mp4_artist_patch_model import patch_mp4_artist


def test_equal_width_patch_changes_only_artist_bytes_and_keeps_other_locale():
    before = _mp4("Old", extra_locale=42)
    after = patch_mp4_artist(before, "New", PROPS)
    offset = before.index(b"Old")
    assert len(after) == len(before)
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [
        offset,
        offset + 1,
        offset + 2,
    ]
    marker = before.index(b"data", before.index(b"zzzz"))
    assert before[marker + 8 : marker + 12] == after[marker + 8 : marker + 12]
    assert after[marker + 8 : marker + 12] == (42).to_bytes(4, "big")
    assert patch_mp4_artist(before, "Old", PROPS) == before
    assert _mp4_structure(after)[1] == "New"


def test_artist_width_counts_utf8_bytes_not_characters():
    before = _mp4("\u00e9x")
    result = patch_mp4_artist(before, "\u540d", PROPS)
    assert _mp4_structure(result)[1] == "\u540d"
    with pytest.raises(Unsupported, match="byte length"):
        patch_mp4_artist(before, "\u00e9", PROPS)


@pytest.mark.parametrize("name", ["", "   ", "\u00a0", "A\x00B", "A\u202eB", "x" * 257])
def test_invalid_artist_rejected(name):
    with pytest.raises(Unsupported):
        patch_mp4_artist(_mp4("Old"), name, PROPS)


def test_unsupported_artist_layout_and_untrusted_properties_fail():
    before = _mp4("Old")
    with pytest.raises(Unsupported):
        patch_mp4_artist(_mp4("Old", artist_extra=_box(b"junk", b"x")), "New", PROPS)
    with pytest.raises(Unsupported):
        patch_mp4_artist(before[:-1], "New", PROPS)
    with pytest.raises(Unsupported):
        patch_mp4_artist(before, "New", replace(PROPS, nlink=2))


def test_opaque_artist_digest_can_become_stale_despite_byte_preservation():
    opaque = _box(b"uuid", bytes(16) + hashlib.sha256(b"Old").digest())
    before = _mp4("Old") + opaque
    after = patch_mp4_artist(before, "New", PROPS)
    assert opaque in after
    assert hashlib.sha256(b"New").digest() != hashlib.sha256(b"Old").digest()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools needed"
)
def test_real_m4a_packet_timing_audio_and_unknown_locale_unchanged(tmp_path):
    from mutagen.mp4 import MP4

    from suno_dl.producer_migration import _audio_signature

    before_file = tmp_path / "before.m4a"
    shutil.copyfile(Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a", before_file)
    audio = MP4(before_file)
    audio["\xa9ART"] = ["Old"]
    audio["zzzz"] = ["sentinel"]
    audio.save()
    before = bytearray(before_file.read_bytes())
    data = before.index(b"data", before.index(b"zzzz"))
    before[data + 8 : data + 12] = (42).to_bytes(4, "big")
    before_file.write_bytes(before)
    after_file = tmp_path / "after.m4a"
    after_file.write_bytes(patch_mp4_artist(bytes(before), "New", PROPS))
    assert MP4(after_file)["\xa9ART"] == ["New"]
    after = after_file.read_bytes()
    assert after[data + 8 : data + 12] == (42).to_bytes(4, "big")
    assert _packet_timing(before_file) == _packet_timing(after_file)
    assert _audio_signature(before_file) == _audio_signature(after_file)
