"""Narrow, test-only metadata allowlist research. Never an authorization check.

ID3 rules are based on the ID3v2.4 native frame document:
https://id3.org/id3v2.4.0-frames

The MP4 scan supports one tightly bounded AAC fixture shape; passing it does
not establish the meaning of opaque audio or metadata payloads.
Apple documents that atom meaning depends on containment:
https://developer.apple.com/documentation/quicktime-file-format/atoms

No function proves file provenance, decoded stream validity, file properties,
all possible trailing metadata, unknown media payload semantics, or durable
publication. The production artist-write guard must remain in place.
"""

from __future__ import annotations

from collections import Counter

from tests.artist_binary_proof_model import (
    MAX_BOXES,
    MAX_BYTES,
    Properties,
    Unsupported,
    _decode_artist,
    _id3_structure,
    verify_id3,
)
from tests.mp4_sample_entry_model import inspect_data_reference, inspect_sample_description

# Deliberately omit user-defined TXXX, pictures, chapters, signatures, links,
# private frames and all frames whose semantics have not been studied.
_ID3_TEXT = {b"TIT2", b"TALB", b"TCON", b"TDRC", b"TRCK", b"TSSE"}

# 65,536 MPEG-1 Layer III frames covers roughly 28 minutes at 44.1 kHz.
# Input size is independently bounded by MAX_BYTES; this is research only.
MAX_MPEG_FRAMES = 65536


def _require_plain_mpeg_frames(audio: bytes) -> int:
    """Accept only contiguous MPEG-1 Layer III frame boundaries, not trailers."""
    bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    rates = (44100, 48000, 32000)
    if not audio or len(audio) > MAX_BYTES:
        raise Unsupported("MPEG input size")
    if any(marker in audio for marker in (b"ID3", b"3DI", b"APETAGEX", b"LYRICSBEGIN")):
        raise Unsupported("possible embedded or trailing metadata")
    cursor = 0
    count = 0
    while cursor < len(audio):
        count += 1
        if count > MAX_MPEG_FRAMES or cursor + 4 > len(audio):
            raise Unsupported("MPEG frame bounds")
        header = int.from_bytes(audio[cursor : cursor + 4], "big")
        if header >> 21 != 0x7FF or (header >> 19) & 3 != 3 or (header >> 17) & 3 != 1:
            raise Unsupported("unsupported MPEG header or trailing data")
        bitrate = (header >> 12) & 15
        sample = (header >> 10) & 3
        if not 1 <= bitrate <= 14 or sample >= 3:
            raise Unsupported("unsupported MPEG bitrate or sample rate")
        length = 144 * bitrates[bitrate] * 1000 // rates[sample] + ((header >> 9) & 1)
        if length < 4 or cursor + length > len(audio):
            raise Unsupported("truncated MPEG frame")
        cursor += length
    if not count:
        raise Unsupported("no MPEG frames")
    return count


def verify_narrow_id3(
    before: bytes, after: bytes, artist: str, old: Properties, new: Properties
) -> None:
    """Research lower bound for plain UTF-8 text fields; not a safety proof."""
    for blob in (before, after):
        frames, audio, _artist, _offset = _id3_structure(blob)
        ids = set()
        for frame_id, raw, _position, _size in frames:
            if frame_id in ids:
                raise Unsupported("duplicate ID3 frame")
            ids.add(frame_id)
            if frame_id == b"TPE1":
                continue
            if frame_id not in _ID3_TEXT or not isinstance(raw, bytes):
                raise Unsupported("unsupported or opaque ID3 frame")
            if raw[8:10] != b"\0\0":
                raise Unsupported("unsupported ID3 frame flags")
            _decode_artist(raw[10:], mp4=False)
        if len(audio) >= 128 and audio[-128:-125] == b"TAG":
            raise Unsupported("ID3v1 trailer requires manual handling")
        if len(audio) >= 32 and audio[-32:-24] == b"APETAGEX":
            raise Unsupported("APE trailer requires manual handling")
        _require_plain_mpeg_frames(audio)
    verify_id3(before, after, artist, old, new)


# Only paths needed to inspect an elementary single-track MP4 hierarchy.
# Unknown atoms anywhere below an inspected container are refused.
_MP4_CONTAINERS = {
    (b"moov",),
    (b"moov", b"trak"),
    (b"moov", b"trak", b"edts"),
    (b"moov", b"trak", b"mdia"),
    (b"moov", b"trak", b"mdia", b"minf"),
    (b"moov", b"trak", b"mdia", b"minf", b"dinf"),
    (b"moov", b"trak", b"mdia", b"minf", b"stbl"),
    (b"moov", b"udta"),
    (b"moov", b"udta", b"meta"),
    (b"moov", b"udta", b"meta", b"ilst"),
}
_MP4_LEAVES = {
    (b"ftyp",),
    (b"free",),
    (b"mdat",),
    (b"moov", b"mvhd"),
    (b"moov", b"trak", b"tkhd"),
    (b"moov", b"trak", b"edts", b"elst"),
    (b"moov", b"trak", b"mdia", b"mdhd"),
    (b"moov", b"trak", b"mdia", b"hdlr"),
    (b"moov", b"trak", b"mdia", b"minf", b"smhd"),
    (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stts"),
    (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsc"),
    (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsz"),
    (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"),
    (b"moov", b"udta", b"meta", b"hdlr"),
}
_MP4_ITEMS = {b"\xa9ART", b"\xa9nam", b"\xa9alb", b"\xa9too"}
_ILST = (b"moov", b"udta", b"meta", b"ilst")


def scan_mp4_allowlist(blob: bytes) -> None:
    """Inspect a small single-track AAC shape, without authorizing writes.

    Sample tables, encoded audio and extensible metadata are not fully validated;
    passing this research scan is not proof that a file is valid or safe.
    """
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_BYTES:
        raise Unsupported("MP4 input size")
    count = 0
    observed: Counter[tuple[bytes, ...]] = Counter()

    def walk(start: int, end: int, parent: tuple[bytes, ...]) -> None:
        nonlocal count
        cursor = start
        while cursor < end:
            count += 1
            if count > MAX_BOXES or cursor + 8 > end:
                raise Unsupported("MP4 atom bound or truncation")
            size = int.from_bytes(blob[cursor : cursor + 4], "big")
            name = blob[cursor + 4 : cursor + 8]
            if size < 8 or cursor + size > end:
                raise Unsupported("unsupported atom size")
            path = (*parent, name)
            observed[path] += 1
            if observed[path] > 1 and path != (b"free",):
                raise Unsupported("duplicate MP4 atom path")
            first, last = cursor + 8, cursor + size
            if path in _MP4_CONTAINERS:
                if name == b"meta":
                    if last - first < 4 or blob[first : first + 4] != bytes(4):
                        raise Unsupported("unsupported meta header")
                    first += 4
                walk(first, last, path)
            elif path == (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
                inspect_sample_description(blob[cursor : cursor + size])
            elif path == (b"moov", b"trak", b"mdia", b"minf", b"dinf", b"dref"):
                inspect_data_reference(blob[cursor : cursor + size])
            elif path == (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sgpd"):
                if blob[first:last] != bytes.fromhex("01000000726f6c6c0000000200000001ffff"):
                    raise Unsupported("unsupported sample group description")
            elif path == (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sbgp"):
                group = blob[first:last]
                if len(group) != 20 or group[:12] != bytes.fromhex("00000000726f6c6c00000001"):
                    raise Unsupported("unsupported sample group mapping")
                if int.from_bytes(group[12:16], "big") < 1 or group[16:20] != bytes.fromhex(
                    "00000001"
                ):
                    raise Unsupported("invalid sample group mapping")
            elif parent == _ILST and name in _MP4_ITEMS:
                if last - first < 16:
                    raise Unsupported("short metadata item")
                child_size = int.from_bytes(blob[first : first + 4], "big")
                if child_size != size - 8 or blob[first + 4 : first + 8] != b"data":
                    raise Unsupported("unsupported metadata child")
                _decode_artist(blob[first + 8 : last], mp4=True)
            elif path not in _MP4_LEAVES:
                raise Unsupported("unsupported or opaque MP4 atom")
            cursor = last

    walk(0, len(blob), ())
    required = {
        (b"ftyp",),
        (b"mdat",),
        (b"moov",),
        (b"moov", b"trak"),
        (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"),
        (b"moov", b"trak", b"mdia", b"minf", b"dinf", b"dref"),
    }
    if not required <= observed.keys():
        raise Unsupported("required MP4 hierarchy is missing or unsupported")
