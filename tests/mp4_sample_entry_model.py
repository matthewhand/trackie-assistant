"""Test-only MP4 sample-entry experiment, not a safety or provenance proof.

This parser only checks a small structural subset from a disposable fixture.
It does not decode AAC, validate all MP4 tables, verify external metadata,
or authorize any production migration or tag update.
"""

from __future__ import annotations

from tests.artist_binary_proof_model import Unsupported

_MAX = 1024 * 1024


def _box(blob: bytes, kind: bytes) -> bytes:
    if len(blob) < 8 or len(blob) > _MAX:
        raise Unsupported("box bounds")
    size = int.from_bytes(blob[:4], "big")
    if size != len(blob) or blob[4:8] != kind:
        raise Unsupported("box type or size")
    return blob[8:]


def _descriptor(data: bytes, tag: int) -> tuple[bytes, bytes]:
    if len(data) < 2 or data[0] != tag:
        raise Unsupported("descriptor type")
    size = 0
    pos = 1
    for _ in range(4):
        if pos == len(data):
            raise Unsupported("descriptor length truncated")
        byte = data[pos]
        pos += 1
        size = (size << 7) | (byte & 127)
        if not byte & 128:
            break
    else:
        raise Unsupported("descriptor length too long")
    if size > len(data) - pos:
        raise Unsupported("descriptor truncated")
    return data[pos : pos + size], data[pos + size :]


def inspect_esds(blob: bytes) -> None:
    payload = _box(blob, b"esds")
    if payload[:4] != bytes(4):
        raise Unsupported("unsupported ESDS fullbox flags")
    es, rest = _descriptor(payload[4:], 3)
    if rest or len(es) < 3 or es[2] != 0:
        raise Unsupported("unsupported ES descriptor")
    config, rest = _descriptor(es[3:], 4)
    if len(config) < 13 or config[0] != 0x40 or config[1] != 0x15:
        raise Unsupported("unsupported decoder configuration")
    specific, remainder = _descriptor(config[13:], 5)
    if not specific or remainder:
        raise Unsupported("unsupported decoder-specific extension")
    sl, rest = _descriptor(rest, 6)
    if sl != b"\x02" or rest:
        raise Unsupported("unsupported SL descriptor")


def inspect_sample_description(stsd: bytes) -> None:
    payload = _box(stsd, b"stsd")
    if len(payload) < 16 or payload[:8] != b"\x00\x00\x00\x00\x00\x00\x00\x01":
        raise Unsupported("unsupported sample description flags or count")
    sample = _box(payload[8:], b"mp4a")
    if (
        len(sample) < 36
        or sample[:6] != bytes(6)
        or sample[6:8] != b"\x00\x01"
        or sample[8:16] != bytes(8)
        or int.from_bytes(sample[16:18], "big") not in (1, 2)
        or int.from_bytes(sample[18:20], "big") != 16
        or sample[20:24] != bytes(4)
        or int.from_bytes(sample[24:28], "big") == 0
    ):
        raise Unsupported("unsupported MP4 audio sample entry")
    inspect_esds(sample[28:])


def inspect_data_reference(dref: bytes) -> None:
    payload = _box(dref, b"dref")
    if payload[:8] != b"\x00\x00\x00\x00\x00\x00\x00\x01":
        raise Unsupported("unsupported reference flags or count")
    url = _box(payload[8:], b"url ")
    if url != b"\x00\x00\x00\x01":
        raise Unsupported("external or unsupported data reference")
