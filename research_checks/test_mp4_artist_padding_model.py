"""Disposable research tests for a bounded MP4 free-padding experiment."""

import hashlib
import shutil
from pathlib import Path

import pytest

from research_checks.test_artist_binary_proof_model import PROPS, _box, _packet_timing
from tests.artist_binary_proof_model import Unsupported, _mp4_structure
from tests.mp4_artist_padding_model import patch_mp4_artist_using_padding


def _fixture(artist="Old", *, padding=64, dirty=False, artist_last=True):
    text = _box(b"data", b"\0\0\0\1\0\0\0\0" + artist.encode())
    art = _box(b"\xa9ART", text)
    other = _box(b"zzzz", _box(b"data", b"\0\0\0\1\0\0\0\x2a" + b"sentinel"))
    items = other + art if artist_last else art + other
    free_data = (b"x" if dirty and padding else b"") + bytes(padding - int(dirty and padding > 0))
    meta = _box(b"meta", bytes(4) + _box(b"ilst", items) + _box(b"free", free_data))
    return _box(b"ftyp", b"M4A ") + _box(b"mdat", b"audio") + _box(b"moov", _box(b"udta", meta))


@pytest.mark.parametrize("artist", ["Producer Name", "A", "Old", "\u540d"])
def test_padding_patch_preserves_unrelated_bytes_and_offsets(artist):
    before = _fixture()
    after = patch_mp4_artist_using_padding(before, artist, PROPS)
    assert len(before) == len(after)
    assert _mp4_structure(after)[1] == artist
    position = before.index(b"zzzz")
    assert after.index(b"zzzz") == position
    other_start = position - 4
    other_size = int.from_bytes(before[other_start:position], "big")
    assert (
        after[other_start : other_start + other_size]
        == before[other_start : other_start + other_size]
    )
    assert (
        before[before.index(b"mdat") : before.index(b"mdat") + 9]
        == (after[after.index(b"mdat") : after.index(b"mdat") + 9])
    )


@pytest.mark.parametrize(
    "before,artist",
    [
        (_fixture(artist_last=False), "Producer"),
        (_fixture(dirty=True), "Producer"),
        (_fixture(padding=0), "A longer producer"),
        (_fixture(), "   "),
        (_fixture(), "A\u202eB"),
        (_fixture()[:-1], "New"),
    ],
)
def test_refuses_unsafe_or_unsupported_input(before, artist):
    with pytest.raises(Unsupported):
        patch_mp4_artist_using_padding(before, artist, PROPS)


def test_opaque_checksum_still_does_not_prove_semantics():
    digest = _box(b"uuid", bytes(16) + hashlib.sha256(b"Old").digest())
    before = _fixture() + digest
    after = patch_mp4_artist_using_padding(before, "Longer artist", PROPS)
    assert digest in after
    assert hashlib.sha256(b"Old").digest() != hashlib.sha256(b"Longer artist").digest()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools needed"
)
def test_copied_real_m4a_preserves_audio_and_unrelated_locale(tmp_path):
    from mutagen.mp4 import MP4

    from suno_dl.producer_migration import _audio_signature

    source = tmp_path / "before.m4a"
    shutil.copyfile(Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a", source)
    audio = MP4(source)
    audio["\xa9ART"] = ["Old"]
    audio["zzzz"] = ["sentinel"]
    audio.save()

    # Construct the research precondition only on this disposable fixture:
    # move artist to the end of the existing ilst without changing its size.
    raw = bytearray(source.read_bytes())
    name = raw.index(b"ilst")
    start = name + 4
    end = name - 4 + int.from_bytes(raw[name - 4 : name], "big")
    items = []
    while start < end:
        size = int.from_bytes(raw[start : start + 4], "big")
        items.append(bytes(raw[start : start + size]))
        start += size
    artist_items = [item for item in items if item[4:8] == b"\xa9ART"]
    assert len(artist_items) == 1
    rearranged = b"".join(item for item in items if item[4:8] != b"\xa9ART") + artist_items[0]
    raw[name + 4 : end] = rearranged
    locale = raw.index(b"data", raw.index(b"zzzz"))
    raw[locale + 8 : locale + 12] = (42).to_bytes(4, "big")
    source.write_bytes(raw)

    before = source.read_bytes()
    result = patch_mp4_artist_using_padding(before, "Verified Producer", PROPS)
    after = tmp_path / "after.m4a"
    after.write_bytes(result)
    assert MP4(after)["\xa9ART"] == ["Verified Producer"]
    assert result[locale + 8 : locale + 12] == (42).to_bytes(4, "big")
    assert _packet_timing(source) == _packet_timing(after)
    assert _audio_signature(source) == _audio_signature(after)
