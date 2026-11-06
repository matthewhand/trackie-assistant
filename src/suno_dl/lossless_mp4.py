"""Lossless metadata editing for nonfragmented MP4 files with a final moov.

Only named iTunes text atoms and explicitly supplied cover art are replaced.
All media bytes, sample tables,
unrelated atoms and their data headers remain byte-identical. Growing the last
metadata subtree cannot shift any media offsets. Other layouts are refused.
This module edits an isolated working copy; publication belongs to its caller.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path


class UnsupportedMP4(ValueError):
    """A file is outside the supported layout."""


_MAX_MOOV = 32 * 1024 * 1024


def _box(kind: bytes, payload: bytes) -> bytes:
    n = len(payload) + 8
    if n >= 2**32:
        raise UnsupportedMP4("box too large")
    return struct.pack(">I4s", n, kind) + payload


def _children(data: bytes) -> list[tuple[bytes, bytes]]:
    result: list[tuple[bytes, bytes]] = []
    pos = 0
    while pos < len(data):
        if len(result) >= 4096 or pos + 8 > len(data):
            raise UnsupportedMP4("truncated or excessive boxes")
        n, kind = struct.unpack_from(">I4s", data, pos)
        if n < 8 or n > len(data) - pos:
            raise UnsupportedMP4("extended, zero or truncated box")
        result.append((kind, data[pos : pos + n]))
        pos += n
    return result


def _unique(items: list[tuple[bytes, bytes]], kind: bytes) -> bytes | None:
    found = [raw for key, raw in items if key == kind]
    if len(found) > 1:
        raise UnsupportedMP4("duplicate metadata container")
    return found[0] if found else None


def _replace(items: list[tuple[bytes, bytes]], kind: bytes, value: bytes) -> bytes:
    return b"".join(value if key == kind else raw for key, raw in items) + (
        b"" if any(key == kind for key, _ in items) else value
    )


def cover_data_type(data: bytes) -> int:
    """Identify bounded JPEG/PNG bytes; callers must decode untrusted images."""
    if not isinstance(data, bytes) or not data or len(data) > 16 * 1024 * 1024:
        raise UnsupportedMP4("invalid cover size")
    if data.startswith(bytes.fromhex("ffd8ff")) and data.endswith(bytes.fromhex("ffd9")):
        return 13
    if data.startswith(bytes.fromhex("89504e470d0a1a0a")) and data.endswith(
        bytes.fromhex("0000000049454e44ae426082")
    ):
        return 14
    raise UnsupportedMP4("cover must be a complete JPEG or PNG image")


def patch_moov(moov: bytes, values: dict[bytes, str], *, cover: bytes | None = None) -> bytes:
    if (not values and cover is None) or set(values) - {b"\xa9ART", b"\xa9nam", b"\xa9lyr"}:
        raise UnsupportedMP4("unsupported tag")
    if (
        not isinstance(moov, bytes)
        or len(moov) < 8
        or moov[4:8] != b"moov"
        or len(moov) > _MAX_MOOV
        or int.from_bytes(moov[:4], "big") != len(moov)
    ):
        raise UnsupportedMP4("invalid movie metadata")
    items = _children(moov[8:])
    if any(k not in {b"mvhd", b"trak", b"udta", b"free"} for k, _ in items):
        raise UnsupportedMP4("fragmented or unsupported movie structure")
    if sum(k == b"mvhd" for k, _ in items) != 1 or sum(k == b"trak" for k, _ in items) != 1:
        raise UnsupportedMP4("only a single track is supported")
    udta = _unique(items, b"udta")
    if udta and any(
        k != b"free"
        for k, _ in items[next(i for i, x in enumerate(items) if x[0] == b"udta") + 1 :]
    ):
        raise UnsupportedMP4("metadata is not the final movie subtree")
    user = _children(udta[8:]) if udta else []
    meta = _unique(user, b"meta")
    if meta and any(
        k != b"free" for k, _ in user[next(i for i, x in enumerate(user) if x[0] == b"meta") + 1 :]
    ):
        raise UnsupportedMP4("metadata is not the final user subtree")
    if meta and meta[8:12] != bytes(4):
        raise UnsupportedMP4("unsupported meta version")
    metadata = (
        _children(meta[12:])
        if meta
        else [(b"hdlr", _box(b"hdlr", bytes(8) + b"mdir" + b"appl" + bytes(9)))]
    )
    ilst = _unique(metadata, b"ilst")
    if ilst and any(
        k != b"free"
        for k, _ in metadata[next(i for i, x in enumerate(metadata) if x[0] == b"ilst") + 1 :]
    ):
        raise UnsupportedMP4("unsupported trailing metadata")
    tags = _children(ilst[8:]) if ilst else []
    for kind, value in values.items():
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024 * 1024:
            raise UnsupportedMP4("invalid tag value")
        old = _unique(tags, kind)
        # Existing text header must be conventional. Unrelated tag headers,
        # including nonzero locales, are preserved as raw bytes.
        if old:
            children = _children(old[8:])
            if (
                len(children) != 1
                or children[0][0] != b"data"
                or children[0][1][8:16] != b"\0\0\0\1\0\0\0\0"
            ):
                raise UnsupportedMP4("unsupported target text atom")
        replacement = _box(kind, _box(b"data", b"\0\0\0\1\0\0\0\0" + value.encode("utf-8")))
        tags = _children(_replace(tags, kind, replacement))
    if cover is not None:
        image_type = cover_data_type(cover)
        old_cover = _unique(tags, b"covr")
        if old_cover:
            children = _children(old_cover[8:])
            if not children or any(
                kind != b"data"
                or len(raw) < 16
                or raw[8:12] not in {bytes.fromhex("0000000d"), bytes.fromhex("0000000e")}
                for kind, raw in children
            ):
                raise UnsupportedMP4("unsupported target cover atom")
        replacement = _box(b"covr", _box(b"data", struct.pack(">II", image_type, 0) + cover))
        tags = _children(_replace(tags, b"covr", replacement))
    new_ilst = _box(b"ilst", b"".join(raw for _, raw in tags))
    new_meta = _box(b"meta", bytes(4) + _replace(metadata, b"ilst", new_ilst))
    new_udta = _box(b"udta", _replace(user, b"meta", new_meta))
    result = _box(b"moov", _replace(items, b"udta", new_udta))
    if len(result) > _MAX_MOOV:
        raise UnsupportedMP4("result metadata too large")
    return result


def _prepare_update(
    stream, values: dict[bytes, str], *, cover: bytes | None = None
) -> tuple[int, bytes]:
    size = os.fstat(stream.fileno()).st_size
    pos = 0
    kinds: list[bytes] = []
    moov_pos = None
    while pos < size:
        if len(kinds) >= 4096:
            raise UnsupportedMP4("excessive top-level boxes")
        stream.seek(pos)
        header = stream.read(8)
        if len(header) != 8:
            raise UnsupportedMP4("truncated file")
        n, kind = struct.unpack(">I4s", header)
        if n < 8 or n > size - pos:
            raise UnsupportedMP4("unsupported top-level box")
        kinds.append(kind)
        if kind == b"moov":
            if pos + n != size or n > _MAX_MOOV:
                raise UnsupportedMP4("moov must be the final box")
            moov_pos = pos
            moov = header + stream.read(n - 8)
        pos += n
    if (
        kinds.count(b"ftyp") != 1
        or kinds.count(b"mdat") != 1
        or kinds.count(b"moov") != 1
        or any(k not in {b"ftyp", b"free", b"mdat", b"moov"} for k in kinds)
    ):
        raise UnsupportedMP4("unsupported top-level structure")
    if moov_pos is None:
        raise UnsupportedMP4("missing moov")
    result = patch_moov(moov, values, cover=cover)
    return moov_pos, result


def validate_mp4_update(path: Path, values: dict[bytes, str]) -> None:
    """Validate the entire supported layout without writing any file."""
    with path.open("rb") as stream:
        _prepare_update(stream, values)


def write_isolated_mp4(path: Path, values: dict[bytes, str], *, cover: bytes | None = None) -> None:
    """Edit only a private staging file. Never pass a live library path."""
    with path.open("r+b") as stream:
        moov_pos, result = _prepare_update(stream, values, cover=cover)
        stream.seek(moov_pos)
        stream.write(result)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
