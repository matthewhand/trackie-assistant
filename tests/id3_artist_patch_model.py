"""Unintegrated in-memory ID3 patch experiment; never a production writer.

Only equal-length UTF-8 ID3v2.4 TPE1 values in the narrow allowlisted model
are supported. It neither opens nor replaces files and makes no claim about
filesystem durability, source provenance, valid audio decoding, or the
semantics of otherwise opaque MPEG frame/ancillary data. The production
tagging guard must remain closed.
"""

from __future__ import annotations

import unicodedata

from tests.artist_allowlist_model import verify_narrow_id3
from tests.artist_binary_proof_model import Properties, Unsupported, _id3_structure


def patch_id3_artist(before: bytes, artist: str, properties: Properties) -> bytes:
    """Produce candidate bytes without mutating inputs; not a publication API."""
    if (
        not isinstance(artist, str)
        or not artist.strip()
        or len(artist) > 256
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in artist)
    ):
        raise Unsupported("invalid artist text")
    encoded = artist.encode("utf-8", "strict")
    if len(encoded) > 512:
        raise Unsupported("artist text exceeds limit")
    frames, _audio, old_artist, _offset = _id3_structure(before)
    # Validates the initial metadata/layout and supplied property snapshot;
    # this is a research check, not a claim about unknown audio semantics.
    verify_narrow_id3(before, before, old_artist, properties, properties)
    found = [item for item in frames if item[0] == b"TPE1"]
    if len(found) != 1:
        raise Unsupported("ambiguous artist frame")
    _, _marker, position, size = found[0]
    payload = before[position + 10 : position + 10 + size]
    body = payload[1:]
    terminated = body.endswith(b"\x00")
    old_raw = body[:-1] if terminated else body
    if len(encoded) != len(old_raw):
        raise Unsupported("artist byte length must remain unchanged")
    out = bytearray(before)
    offset = position + 11
    out[offset : offset + len(old_raw)] = encoded
    result = bytes(out)
    verify_narrow_id3(before, result, artist, properties, properties)
    return result
