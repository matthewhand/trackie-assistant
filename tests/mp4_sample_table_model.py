"""Test-only MP4 AAC sample-table experiment; never authorizes writes.

Checks one narrow single-track fixture: every declared chunk must be inside the
one mdat payload and every sample must be counted by the timing/chunk tables.
This does not decode AAC, establish producer identity, validate all timing or
opaque metadata semantics, or prove safe publication. Production stays gated.
"""

from __future__ import annotations

from tests.artist_allowlist_model import scan_mp4_allowlist
from tests.artist_binary_proof_model import Unsupported

_MAX_ENTRIES = 200_000


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise Unsupported("truncated box")
        size = int.from_bytes(data[pos : pos + 4], "big")
        if size < 8 or pos + size > end:
            raise Unsupported("invalid box size")
        yield data[pos + 4 : pos + 8], pos, pos + size
        pos += size


def _one(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int]:
    matches = [(left, right) for name, left, right in _boxes(data, start, end) if name == kind]
    if len(matches) != 1:
        raise Unsupported("missing or ambiguous box")
    return matches[0]


def _table(data: bytes, start: int, end: int, width: int) -> list[tuple[int, ...]]:
    payload = data[start + 8 : end]
    if len(payload) < 8 or payload[:4] != bytes(4):
        raise Unsupported("unsupported sample table header")
    count = int.from_bytes(payload[4:8], "big")
    if count < 1 or count > _MAX_ENTRIES or len(payload) != 8 + count * width * 4:
        raise Unsupported("invalid sample table count")
    return [
        tuple(int.from_bytes(payload[8 + 4 * j : 12 + 4 * j], "big") for j in range(i, i + width))
        for i in range(0, count * width, width)
    ]


def inspect_sample_tables(data: bytes) -> tuple[int, int]:
    """Return sample and chunk counts for a bounded, self-contained AAC shape."""
    scan_mp4_allowlist(data)
    mdat_start, mdat_end = _one(data, 0, len(data), b"mdat")
    start, end = _one(data, 0, len(data), b"moov")
    for name in (b"trak", b"mdia", b"minf", b"stbl"):
        start, end = _one(data, start + 8, end, name)
    tables = {}
    for name, width in ((b"stco", 1), (b"stsc", 3), (b"stts", 2)):
        left, right = _one(data, start + 8, end, name)
        tables[name] = _table(data, left, right, width)
    left, right = _one(data, start + 8, end, b"stsz")
    payload = data[left + 8 : right]
    if len(payload) < 12 or payload[:4] != bytes(4):
        raise Unsupported("unsupported sample sizes")
    fixed = int.from_bytes(payload[4:8], "big")
    count = int.from_bytes(payload[8:12], "big")
    if count < 1 or count > _MAX_ENTRIES:
        raise Unsupported("invalid sample count")
    if fixed:
        if len(payload) != 12:
            raise Unsupported("invalid fixed sample sizes")
        sizes = [fixed] * count
    else:
        if len(payload) != 12 + count * 4:
            raise Unsupported("invalid sample sizes length")
        sizes = [int.from_bytes(payload[12 + i * 4 : 16 + i * 4], "big") for i in range(count)]
        if any(size == 0 for size in sizes):
            raise Unsupported("empty sample")
    group_descriptions = [
        (left, right) for name, left, right in _boxes(data, start + 8, end) if name == b"sgpd"
    ]
    group_mapping = [
        (left, right) for name, left, right in _boxes(data, start + 8, end) if name == b"sbgp"
    ]
    if bool(group_descriptions) != bool(group_mapping):
        raise Unsupported("incomplete sample grouping")
    if group_mapping:
        group_start, _ = group_mapping[0]
        if int.from_bytes(data[group_start + 20 : group_start + 24], "big") != count:
            raise Unsupported("sample group/count mismatch")
    timing = tables[b"stts"]
    if any(n == 0 or duration == 0 for n, duration in timing) or sum(n for n, _ in timing) != count:
        raise Unsupported("timing/sample mismatch")
    mapping = tables[b"stsc"]
    chunks = tables[b"stco"]
    if mapping[0][0] != 1 or any(
        first < 1 or per_chunk < 1 or description != 1 for first, per_chunk, description in mapping
    ):
        raise Unsupported("invalid chunk mapping")
    if any(a[0] >= b[0] for a, b in zip(mapping, mapping[1:], strict=False)):
        raise Unsupported("unordered chunk mapping")
    if mapping[-1][0] > len(chunks):
        raise Unsupported("chunk mapping exceeds chunks")
    sample = 0
    mapping_index = 0
    previous_end = mdat_start + 8
    for index, (offset,) in enumerate(chunks, 1):
        if mapping_index + 1 < len(mapping) and index >= mapping[mapping_index + 1][0]:
            mapping_index += 1
        per_chunk = mapping[mapping_index][1]
        if sample + per_chunk > count:
            raise Unsupported("chunk contains too many samples")
        end_offset = offset + sum(sizes[sample : sample + per_chunk])
        if offset < previous_end or end_offset > mdat_end:
            raise Unsupported("chunk outside mdat or overlaps")
        previous_end = end_offset
        sample += per_chunk
    if sample != count:
        raise Unsupported("unmapped samples")
    return count, len(chunks)
