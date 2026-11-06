"""Tests for test-only artist metadata byte proof; no production integration."""

import hashlib
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.artist_binary_proof_model import Properties, Unsupported, verify_id3, verify_mp4

PROPS = Properties(1000, 1000, stat.S_IFREG | 0o640, 1, 0, ())


def _box(name, data):
    return (len(data) + 8).to_bytes(4, "big") + name + data


def _mp4(artist, *, extra_locale=42, mdat=b"audio", artist_extra=b""):
    text = _box(b"data", b"\0\0\0\1\0\0\0\0" + artist.encode())
    other = _box(b"zzzz", _box(b"data", b"\0\0\0\1" + extra_locale.to_bytes(4, "big") + b"value"))
    ilst = _box(b"ilst", _box(b"\xa9ART", text + artist_extra) + other)
    return (
        _box(b"ftyp", b"M4A ")
        + _box(b"mdat", mdat)
        + _box(b"moov", _box(b"udta", _box(b"meta", b"\0\0\0\0" + ilst)))
    )


def _syncsafe(n):
    return bytes(((n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127))


def _frame(name, payload, flags=b"\0\0"):
    return name + _syncsafe(len(payload)) + flags + payload


def _mp3(artist, *, status=0x40, audio=b"audio", padding=8):
    body = _frame(b"TPE1", b"\3" + artist.encode())
    body += _frame(b"TXXX", b"\3name\0value", bytes((status, 0)))
    body += bytes(padding)
    return b"ID3\4\0\0" + _syncsafe(len(body)) + body + audio


def test_mp4_exact_structure_allows_only_artist_change():
    verify_mp4(_mp4("Old"), _mp4("New"), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New Artist"), "New Artist", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New", extra_locale=0), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New", mdat=b"changed"), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New", artist_extra=_box(b"junk", b"x")), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New"), "Other", PROPS, PROPS)


def test_mp4_rejects_unsupported_sizes_and_moved_mdat():
    before = _mp4("Old")
    with pytest.raises(Unsupported):
        verify_mp4(before, b"\0\0\0\1" + before[4:], "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_mp4(before, before[:-1], "Old", PROPS, PROPS)
    after = _box(b"free", b"moved") + _mp4("New")
    with pytest.raises(Unsupported):
        verify_mp4(before, after, "New", PROPS, PROPS)


def test_id3_frame_flags_and_audio_are_preserved():
    verify_id3(_mp3("Old"), _mp3("New"), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(_mp3("Old"), _mp3("New Artist", padding=1), "New Artist", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(_mp3("Old"), _mp3("New", status=0), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(_mp3("Old"), _mp3("New", audio=b"changed"), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(_mp3("Old"), _mp3("New"), "Other", PROPS, PROPS)


def test_id3_rejects_header_flags_invalid_sizes_and_multiple_artists():
    before = _mp3("Old")
    with pytest.raises(Unsupported):
        verify_id3(before, before[:5] + b"\x80" + before[6:], "Old", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(before, before[:6] + b"\xff\x00\x00\x00" + before[10:], "Old", PROPS, PROPS)
    body = _frame(b"TPE1", b"\3Old") + _frame(b"TPE1", b"\3New")
    bad = b"ID3\4\0\0" + _syncsafe(len(body)) + body + b"audio"
    with pytest.raises(Unsupported):
        verify_id3(before, bad, "New", PROPS, PROPS)


@pytest.mark.parametrize(
    "changed",
    [
        replace(PROPS, uid=1001),
        replace(PROPS, gid=1001),
        replace(PROPS, mode=stat.S_IFREG | 0o600),
        replace(PROPS, nlink=2),
        replace(PROPS, inode_flags=0x40),
        replace(PROPS, xattrs=(("user.x", b"x"),)),
    ],
)
def test_changed_file_properties_are_refused(changed):
    with pytest.raises(Unsupported):
        verify_mp4(_mp4("Old"), _mp4("New"), "New", PROPS, changed)
    with pytest.raises(Unsupported):
        verify_id3(_mp3("Old"), _mp3("New"), "New", PROPS, changed)


def _packet_timing(path):
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_packets",
            "-show_streams",
            "-show_entries",
            "packet=stream_index,pts,dts,duration,size,flags,data_hash:"
            "stream=index,codec_name,codec_type,time_base,sample_rate,channels,start_pts,duration_ts",
            "-show_data_hash",
            "sha256",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout
    assert len(output) < 2 * 1024 * 1024  # fixture-only, not a production bound
    return output


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe needed for real fixture")
def test_real_mp4_artist_only_and_unrelated_locale_loss(tmp_path):
    from mutagen.mp4 import MP4

    path = tmp_path / "sample.m4a"
    shutil.copyfile(Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a", path)
    audio = MP4(path)
    audio["\u00a9ART"] = ["Old"]
    audio["zzzz"] = ["sentinel"]
    audio.save()
    pristine = path.read_bytes()
    old = tmp_path / "old.m4a"
    old.write_bytes(pristine)
    audio = MP4(path)
    audio["\u00a9ART"] = ["New"]
    audio.save()
    verify_mp4(pristine, path.read_bytes(), "New", PROPS, PROPS)
    assert _packet_timing(old) == _packet_timing(path)

    raw = bytearray(pristine)
    location = raw.index(b"data", raw.index(b"zzzz"))
    raw[location + 8 : location + 12] = (42).to_bytes(4, "big")
    path.write_bytes(raw)
    audio = MP4(path)
    audio["\u00a9ART"] = ["New"]
    audio.save()
    with pytest.raises(Unsupported):
        verify_mp4(bytes(raw), path.read_bytes(), "New", PROPS, PROPS)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools needed"
)
def test_real_mp3_artist_only_and_unrelated_frame_flag_loss(tmp_path):
    from mutagen.id3 import ID3, TPE1, TXXX

    path = tmp_path / "sample.mp3"
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
    tags.add(TXXX(encoding=3, desc="Sentinel", text=["hello"]))
    tags.save(path)
    pristine = path.read_bytes()
    old = tmp_path / "old.mp3"
    old.write_bytes(pristine)
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["New"]))
    tags.save(path)
    verify_id3(pristine, path.read_bytes(), "New", PROPS, PROPS)
    assert _packet_timing(old) == _packet_timing(path)

    raw = bytearray(pristine)
    location = raw.index(b"TXXX")
    raw[location + 8] = 0x40
    path.write_bytes(raw)
    tags = ID3(path)
    tags.add(TPE1(encoding=3, text=["New"]))
    tags.save(path)
    with pytest.raises(Unsupported):
        verify_id3(bytes(raw), path.read_bytes(), "New", PROPS, PROPS)


def test_id3_preserves_audio_offset_for_absolute_chapter_references():
    # A CHAP start/end offset is absolute from the beginning of the file;
    # identical frame and audio bytes alone do not preserve its meaning if
    # changing the tag shifts where audio begins.
    chap = _frame(
        b"CHAP",
        b"c1\x00" + b"\x00\x00\x00\x00" * 2 + (91).to_bytes(4, "big") * 2,
    )

    def file_with_chapter(artist):
        body = _frame(b"TPE1", b"\x03" + artist.encode()) + chap + bytes(8)
        return b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body + bytes(range(100))

    verify_id3(file_with_chapter("Old"), file_with_chapter("New"), "New", PROPS, PROPS)
    with pytest.raises(Unsupported):
        verify_id3(
            file_with_chapter("Old"),
            file_with_chapter("A much longer name"),
            "A much longer name",
            PROPS,
            PROPS,
        )


def test_mp4_opaque_absolute_offset_cannot_shift_with_artist_growth():
    before_body = _mp4("Old")
    uuid_size = 8 + 16 + 8
    sentinel_offset = len(before_body) + uuid_size
    opaque = _box(b"uuid", bytes(16) + sentinel_offset.to_bytes(8, "big"))
    trailing = _box(b"free", b"sentinel")
    before = before_body + opaque + trailing
    after = _mp4("Much longer") + opaque + trailing
    assert before[sentinel_offset + 8 : sentinel_offset + 16] == b"sentinel"
    assert after[sentinel_offset + 8 : sentinel_offset + 16] != b"sentinel"
    with pytest.raises(Unsupported):
        verify_mp4(before, after, "Much longer", PROPS, PROPS)


def test_unchanged_opaque_digest_may_become_semantically_stale():
    # The prototype can verify equal bytes, not whether an opaque owner meant
    # those bytes as a checksum of the artist field. This is why a model pass
    # cannot authorize a production write.
    opaque = _box(b"uuid", bytes(16) + hashlib.sha256(b"Old").digest())
    before = _mp4("Old") + opaque
    after = _mp4("New") + opaque
    verify_mp4(before, after, "New", PROPS, PROPS)
    assert hashlib.sha256(b"Old").digest() != hashlib.sha256(b"New").digest()
