"""Targeted ID3 and RIFF edits on private staging files.

Unrelated frames/chunks are copied verbatim. Existing MP3 tags keep their
original total size, so audio offsets never move. Unsupported flags, duplicate
targets, additional tag locations and insufficient padding fail closed.
Publication and recovery are the caller's responsibility.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

MAX_TAG = 16 * 1024 * 1024


class UnsupportedID3(ValueError):
    """The file cannot be changed by this restricted writer."""


def syncsafe(raw: bytes) -> int:
    if len(raw) != 4 or any(x & 0x80 for x in raw):
        raise UnsupportedID3("invalid synchsafe integer")
    return sum(value << shift for value, shift in zip(raw, (21, 14, 7, 0), strict=True))


def encode_size(value: int) -> bytes:
    if not 0 <= value < 2**28:
        raise UnsupportedID3("tag exceeds size bound")
    return bytes((value >> shift) & 127 for shift in (21, 14, 7, 0))


def patch_id3(tag: bytes | None, values: dict[bytes, str]) -> bytes:
    if not values or set(values) - {b"TPE1", b"TIT2"}:
        raise UnsupportedID3("unsupported target frame")
    frames: list[tuple[bytes, bytes]] = []
    if tag is None:
        version = 4
        capacity = None
        header = b"ID3\x04\x00\x00"
    else:
        if len(tag) < 10 or tag[:3] != b"ID3" or tag[3:6] not in {b"\x03\0\0", b"\x04\0\0"}:
            raise UnsupportedID3("only plain ID3v2.3/v2.4 headers are supported")
        version = tag[3]
        capacity = syncsafe(tag[6:10])
        if capacity > MAX_TAG or capacity != len(tag) - 10:
            raise UnsupportedID3("invalid tag size")
        header = tag[:6]
        pos = 10
        while pos < len(tag):
            if tag[pos] == 0:
                if any(tag[pos:]):
                    raise UnsupportedID3("nonzero tag padding")
                break
            if len(frames) >= 4096 or pos + 10 > len(tag):
                raise UnsupportedID3("truncated or excessive frames")
            kind = tag[pos : pos + 4]
            if any(not (65 <= b <= 90 or 48 <= b <= 57) for b in kind):
                raise UnsupportedID3("invalid frame identifier")
            n = (
                syncsafe(tag[pos + 4 : pos + 8])
                if version == 4
                else int.from_bytes(tag[pos + 4 : pos + 8], "big")
            )
            end = pos + 10 + n
            if n == 0 or end > len(tag):
                raise UnsupportedID3("invalid frame size")
            if kind in {b"SEEK", b"SIGN"}:
                raise UnsupportedID3("additional tags or signature require manual handling")
            frames.append((kind, tag[pos:end]))
            pos = end
    for kind, value in values.items():
        if (
            not isinstance(value, str)
            or not value
            or "\0" in value
            or len(value.encode("utf-8")) > 4096
        ):
            raise UnsupportedID3("invalid text value")
        found = [raw for key, raw in frames if key == kind]
        if len(found) > 1 or (found and found[0][8:10] != bytes(2)):
            raise UnsupportedID3("duplicate or flagged target frame")
        if version == 4:
            payload = b"\x03" + value.encode("utf-8")
        else:
            try:
                payload = b"\x00" + value.encode("latin-1")
            except UnicodeError as exc:
                raise UnsupportedID3("v2.3 text requires unsupported unsynchronisation") from exc
            if any(
                payload[i] == 255
                and (i + 1 == len(payload) or payload[i + 1] == 0 or payload[i + 1] >= 224)
                for i in range(len(payload))
            ):
                raise UnsupportedID3("v2.3 text requires unsynchronisation")
        size = encode_size(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
        replacement = kind + size + bytes(2) + payload
        frames = [(key, replacement if key == kind else raw) for key, raw in frames]
        if not found:
            frames.append((kind, replacement))
    body = b"".join(raw for _, raw in frames)
    if capacity is None:
        capacity = max(1024, len(body) + 256)
    if len(body) > capacity:
        raise UnsupportedID3("insufficient fixed tag padding")
    return header + encode_size(capacity) + body + bytes(capacity - len(body))


def _mp3_update(stream, values: dict[bytes, str]) -> tuple[int, bytes]:
    size = os.fstat(stream.fileno()).st_size
    if size < 10:
        raise UnsupportedID3("short MP3")
    stream.seek(max(0, size - 160))
    tail = stream.read()
    if tail[-128:-125] == b"TAG" or b"APETAGEX" in tail or tail[-10:-7] == b"3DI":
        raise UnsupportedID3("additional metadata tag location")
    stream.seek(0)
    header = stream.read(10)
    if not header.startswith(b"ID3"):
        # Prepending an ID3 tag could invalidate offsets in another tagging
        # system. Only existing fixed-size tags are eligible here.
        raise UnsupportedID3("MP3 requires an existing supported ID3 tag")
    n = syncsafe(header[6:10])
    if n > MAX_TAG or 10 + n >= size:
        raise UnsupportedID3("invalid MP3 tag size")
    return 0, patch_id3(header + stream.read(n), values)


def _wav_update(stream, values: dict[bytes, str]) -> tuple[int, bytes, int]:
    size = os.fstat(stream.fileno()).st_size
    stream.seek(0)
    header = stream.read(12)
    if (
        len(header) != 12
        or header[:4] != b"RIFF"
        or header[8:] != b"WAVE"
        or int.from_bytes(header[4:8], "little") != size - 8
    ):
        raise UnsupportedID3("unsupported RIFF/WAVE container")
    pos = 12
    tags = []
    data_count = fmt_count = 0
    count = 0
    while pos < size:
        count += 1
        if count > 4096 or pos + 8 > size:
            raise UnsupportedID3("invalid WAV chunks")
        stream.seek(pos)
        kind, n = struct.unpack("<4sI", stream.read(8))
        end = pos + 8 + n + (n % 2)
        if end > size:
            raise UnsupportedID3("truncated WAV chunk")
        data_count += kind == b"data"
        fmt_count += kind == b"fmt "
        if kind in {b"id3 ", b"ID3 "}:
            if n > MAX_TAG or end != size:
                raise UnsupportedID3("ID3 must be the final WAV chunk")
            content = stream.read(n)
            if n % 2 and stream.read(1) != b"\0":
                raise UnsupportedID3("nonzero ID3 chunk padding")
            tags.append((pos, kind, content))
        if kind == b"LIST":
            if n > MAX_TAG:
                raise UnsupportedID3("oversized RIFF metadata")
            content = stream.read(n)
            if content[:4] == b"INFO":
                cursor = 4
                while cursor < len(content):
                    if cursor + 8 > len(content):
                        raise UnsupportedID3("truncated INFO metadata")
                    key = content[cursor : cursor + 4]
                    length = int.from_bytes(content[cursor + 4 : cursor + 8], "little")
                    if key in {b"IART", b"INAM"}:
                        raise UnsupportedID3("additional artist/title tagging system")
                    cursor += 8 + length + length % 2
                    if cursor > len(content):
                        raise UnsupportedID3("invalid INFO chunk")
        pos = end
    if data_count != 1 or fmt_count != 1 or len(tags) > 1:
        raise UnsupportedID3("ambiguous WAV structure")
    offset, kind, tag = tags[0] if tags else (size, b"id3 ", None)
    patched = patch_id3(tag, values)
    chunk = struct.pack("<4sI", kind, len(patched)) + patched + bytes(len(patched) % 2)
    new_size = offset + len(chunk)
    if new_size - 8 >= 2**32:
        raise UnsupportedID3("WAV exceeds RIFF size")
    return offset, chunk, new_size


def validate_artist_update(path: Path, extension: str, artist: str) -> None:
    with path.open("rb") as stream:
        if extension == ".mp3":
            _mp3_update(stream, {b"TPE1": artist})
        elif extension == ".wav":
            _wav_update(stream, {b"TPE1": artist})
        else:
            raise UnsupportedID3("unsupported file extension")


def write_isolated_artist(path: Path, extension: str, artist: str) -> None:
    """Only edit private staging. The caller owns the publication journal."""
    with path.open("r+b") as stream:
        if extension == ".mp3":
            offset, data = _mp3_update(stream, {b"TPE1": artist})
            stream.seek(offset)
            stream.write(data)
        elif extension == ".wav":
            offset, data, size = _wav_update(stream, {b"TPE1": artist})
            stream.seek(offset)
            stream.write(data)
            stream.truncate(size)
            stream.seek(4)
            stream.write((size - 8).to_bytes(4, "little"))
        else:
            raise UnsupportedID3("unsupported file extension")
        stream.flush()
        os.fsync(stream.fileno())
