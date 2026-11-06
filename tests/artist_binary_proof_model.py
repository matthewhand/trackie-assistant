"""Byte-structure research model, never authorization to retag production files.

The model accepts only tiny, narrowly supported MP4/ID3 layouts. It compares
all non-artist structures byte-for-byte with atom and frame positions fixed.
It normalizes only zero ID3 padding. It does not open files, attest source provenance,
measure packets, verify filesystem durability or prevent races. Supplied file
properties must be trustworthy snapshots; atime, ctime, birthtime and mtime
are not checked. It cannot establish creator identity or the semantics of
opaque checksums and signatures. A pass is necessary
research evidence, never a complete safety
proof or grounds to disable the production retag guard.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass

MAX_BYTES = 32 * 1024 * 1024
MAX_BOXES = 4096


class Unsupported(ValueError):
    """Structure is unsafe, unsupported, or differs beyond the allowed field."""


@dataclass(frozen=True)
class Properties:
    uid: int
    gid: int
    mode: int
    nlink: int
    inode_flags: int
    xattrs: tuple[tuple[str, bytes], ...]


def _properties_match(before: Properties, after: Properties) -> None:
    if (
        not isinstance(before, Properties)
        or not isinstance(after, Properties)
        or before != after
        or before.nlink != 1
        or not stat.S_ISREG(before.mode)
    ):
        raise Unsupported("file properties differ or are untrusted")


def _decode_artist(payload: bytes, *, mp4: bool) -> str:
    if mp4:
        # iTunes data atom: only UTF-8 text, zero locale; no hidden children.
        if len(payload) < 8 or payload[:8] != b"\x00\x00\x00\x01\x00\x00\x00\x00":
            raise Unsupported("unsupported MP4 artist encoding or locale")
        raw = payload[8:]
    else:
        # ID3v2.4 UTF-8 TPE1 only, no multi-valued artist fields.
        if not payload or payload[0] != 3:
            raise Unsupported("unsupported ID3 artist encoding")
        raw = payload[1:]
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
    if not raw or b"\x00" in raw:
        raise Unsupported("empty or multiple artists")
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise Unsupported("invalid artist UTF-8") from exc


def _mp4_structure(blob: bytes) -> tuple[tuple, str, int]:
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_BYTES:
        raise Unsupported("MP4 input size")
    count = 0
    artists = []
    mdats = []

    def children(start: int, end: int, path: tuple[bytes, ...]) -> tuple:
        nonlocal count
        items = []
        cursor = start
        while cursor < end:
            count += 1
            if count > MAX_BOXES or cursor + 8 > end:
                raise Unsupported("MP4 atom bound or truncation")
            size = int.from_bytes(blob[cursor : cursor + 4], "big")
            name = blob[cursor + 4 : cursor + 8]
            if size < 8 or cursor + size > end:
                raise Unsupported("unsupported extended, zero, or truncated atom")
            next_path = path + (name,)
            payload_start, atom_end = cursor + 8, cursor + size
            item: tuple
            if next_path == (b"moov",) or next_path == (b"moov", b"udta"):
                item = (name, children(payload_start, atom_end, next_path))
            elif next_path == (b"moov", b"udta", b"meta"):
                if size < 12:
                    raise Unsupported("short MP4 meta atom")
                prefix = blob[payload_start : payload_start + 4]
                item = (name, prefix, children(payload_start + 4, atom_end, next_path))
            elif next_path == (b"moov", b"udta", b"meta", b"ilst"):
                item = (name, children(payload_start, atom_end, next_path))
            elif next_path == (b"moov", b"udta", b"meta", b"ilst", b"\xa9ART"):
                if size < 24:
                    raise Unsupported("short MP4 artist atom")
                subsize = int.from_bytes(blob[payload_start : payload_start + 4], "big")
                if subsize != size - 8 or blob[payload_start + 4 : payload_start + 8] != b"data":
                    raise Unsupported("MP4 artist has extra or unsupported children")
                artists.append(_decode_artist(blob[payload_start + 8 : atom_end], mp4=True))
                item = (name, "artist")
            else:
                if not path and name == b"mdat":
                    mdats.append(cursor)
                item = (name, blob[cursor:atom_end])
            items.append((cursor, size, item))
            cursor = atom_end
        if cursor != end:
            raise Unsupported("MP4 atom boundary")
        return tuple(items)

    structure = children(0, len(blob), ())
    names = [entry[2][0] for entry in structure]
    if names.count(b"ftyp") != 1 or names.count(b"moov") != 1 or len(mdats) != 1:
        raise Unsupported("unsupported MP4 top-level structure")
    if len(artists) != 1:
        raise Unsupported("MP4 must contain exactly one supported artist")
    return structure, artists[0], mdats[0]


def verify_mp4(
    before: bytes, after: bytes, expected_artist: str, old: Properties, new: Properties
) -> None:
    _properties_match(old, new)
    first, _, first_media_offset = _mp4_structure(before)
    second, artist, second_media_offset = _mp4_structure(after)
    if (
        len(before) != len(after)
        or first != second
        or first_media_offset != second_media_offset
        or artist != expected_artist
    ):
        raise Unsupported("MP4 non-artist bytes, media offset, or artist differ")


def _syncsafe(raw: bytes) -> int:
    if len(raw) != 4 or any(x & 0x80 for x in raw):
        raise Unsupported("invalid syncsafe integer")
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _id3_structure(blob: bytes) -> tuple[tuple, bytes, str, int]:
    if not isinstance(blob, bytes) or not 10 <= len(blob) <= MAX_BYTES:
        raise Unsupported("ID3 input size")
    if blob[:6] != b"ID3\x04\x00\x00":
        raise Unsupported("only plain ID3v2.4 is supported")
    end = 10 + _syncsafe(blob[6:10])
    if end > len(blob):
        raise Unsupported("truncated ID3 tag")
    frames: list[tuple] = []
    artists = []
    cursor = 10
    while cursor < end:
        if blob[cursor] == 0:
            if any(blob[cursor:end]):
                raise Unsupported("nonzero ID3 padding")
            break
        if cursor + 10 > end or len(frames) >= MAX_BOXES:
            raise Unsupported("ID3 frame bounds")
        frame_id = blob[cursor : cursor + 4]
        if any(not (48 <= char <= 57 or 65 <= char <= 90) for char in frame_id):
            raise Unsupported("unsupported ID3 frame identifier")
        size = _syncsafe(blob[cursor + 4 : cursor + 8])
        frame_end = cursor + 10 + size
        if frame_end > end:
            raise Unsupported("truncated ID3 frame")
        if frame_id == b"TPE1":
            if blob[cursor + 8 : cursor + 10] != b"\x00\x00":
                raise Unsupported("unsupported ID3 artist frame flags")
            artists.append(_decode_artist(blob[cursor + 10 : frame_end], mp4=False))
            frames.append((frame_id, "artist", cursor, size))
        else:
            frames.append((frame_id, blob[cursor:frame_end], cursor, size))
        cursor = frame_end
    if len(artists) != 1 or end == len(blob):
        raise Unsupported("ID3 requires one artist and a nonempty audio payload")
    return tuple(frames), blob[end:], artists[0], end


def verify_id3(
    before: bytes, after: bytes, expected_artist: str, old: Properties, new: Properties
) -> None:
    _properties_match(old, new)
    frames1, audio1, _, offset1 = _id3_structure(before)
    frames2, audio2, artist, offset2 = _id3_structure(after)
    if frames1 != frames2 or audio1 != audio2 or offset1 != offset2 or artist != expected_artist:
        raise Unsupported("ID3 non-artist frame, audio bytes or artist differ")
