"""Byte-level regressions for restricted ID3/RIFF staging writes."""

import struct

import pytest

from suno_dl.lossless_id3 import UnsupportedID3, encode_size, patch_id3, write_isolated_artist


def frame(kind, payload, flags=b"\0\0", version=4):
    size = encode_size(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
    return kind + size + flags + payload


def tag(*frames, version=4, padding=1024):
    body = b"".join(frames) + bytes(padding)
    return b"ID3" + bytes([version, 0, 0]) + encode_size(len(body)) + body


def test_nonartist_payload_and_status_flags_survive():
    other = frame(b"TXXX", b"\x03Sentinel\0opaque bytes", b"\x40\0")
    before = tag(frame(b"TPE1", b"\x03Old"), other)
    after = patch_id3(before, {b"TPE1": "Producer \u03bb"})
    assert len(after) == len(before) and other in after


@pytest.mark.parametrize(
    "source",
    [
        tag(frame(b"TPE1", b"\x03Old"), frame(b"TPE1", b"\x03Other")),
        tag(frame(b"TPE1", b"\x03Old", b"\x10\0")),
        tag(frame(b"TPE1", b"\x03Old"), padding=0),
        tag(frame(b"SIGN", b"signature"), frame(b"TPE1", b"\x03Old")),
        b"ID3\x04\x00\x80" + bytes(4),
    ],
)
def test_unsupported_tag_never_mutates_mp3(tmp_path, source):
    p = tmp_path / "test.mp3"
    p.write_bytes(source + b"audio" * 1000)
    before = p.read_bytes()
    with pytest.raises(UnsupportedID3):
        write_isolated_artist(p, ".mp3", "A much longer producer")
    assert p.read_bytes() == before


def test_v23_preserves_version_and_frame_bytes():
    other = frame(b"TXXX", b"\x00Sentinel\0opaque", b"\x80\0", version=3)
    before = tag(frame(b"TPE1", b"\x00Old", version=3), other, version=3)
    after = patch_id3(before, {b"TPE1": "Producer"})
    assert after[:10] == before[:10] and other in after
    with pytest.raises(UnsupportedID3):
        patch_id3(before, {b"TPE1": "\u03bb"})


@pytest.mark.parametrize("tail", [b"TAG" + bytes(125), b"APETAGEX" + bytes(24), b"3DI" + bytes(7)])
def test_additional_mp3_tag_locations_refused(tmp_path, tail):
    p = tmp_path / "test.mp3"
    p.write_bytes(tag(frame(b"TPE1", b"\x03Old")) + b"audio" * 1000 + tail)
    before = p.read_bytes()
    with pytest.raises(UnsupportedID3):
        write_isolated_artist(p, ".mp3", "Producer")
    assert p.read_bytes() == before


def riff(*chunks):
    body = b"WAVE" + b"".join(
        kind + struct.pack("<I", len(value)) + value + bytes(len(value) % 2)
        for kind, value in chunks
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_wav_conflicting_info_artist_refused(tmp_path):
    info = b"INFO" + b"IART" + struct.pack("<I", 4) + b"Old\0"
    p = tmp_path / "test.wav"
    p.write_bytes(riff((b"fmt ", bytes(16)), (b"LIST", info), (b"data", b"audio" * 1000)))
    before = p.read_bytes()
    with pytest.raises(UnsupportedID3):
        write_isolated_artist(p, ".wav", "Producer")
    assert p.read_bytes() == before


def test_wav_keeps_all_original_chunks_in_place_when_appending_id3(tmp_path):
    raw = riff((b"fmt ", bytes(16)), (b"JUNK", b"opaque sentinel"), (b"data", b"audio" * 1000))
    p = tmp_path / "test.wav"
    p.write_bytes(raw)
    write_isolated_artist(p, ".wav", "Producer")
    after = p.read_bytes()
    assert after[:4] == raw[:4] and after[8 : len(raw)] == raw[8:]
    assert len(after) > len(raw)
