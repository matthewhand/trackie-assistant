"""Unintegrated in-memory fixed-width MP4 artist patch research.

This is not a production writer or authorization check. It cannot establish
producer provenance, verify unknown container/decoder semantics, rule out
opaque checksums of artist data, prove audio decode validity, attest file
properties, guard concurrent writes, or safely publish a file. The production
embedded-tag guard must remain closed.
"""

from __future__ import annotations

import unicodedata

from tests.artist_binary_proof_model import Properties, Unsupported, _mp4_structure, verify_mp4

_PATH = (b"moov", b"udta", b"meta", b"ilst", b"\xa9ART")


def _artist_atom(structure: tuple) -> tuple[int, int]:
    found = []

    def visit(entries: tuple, depth: int) -> None:
        for position, size, item in entries:
            if item[0] != _PATH[depth]:
                continue
            if depth == len(_PATH) - 1:
                if item != (b"\xa9ART", "artist"):
                    raise Unsupported("invalid artist atom")
                found.append((position, size))
            else:
                children = item[-1]
                if not isinstance(children, tuple):
                    raise Unsupported("invalid artist container")
                visit(children, depth + 1)

    visit(structure, 0)
    if len(found) != 1:
        raise Unsupported("ambiguous artist atom")
    return found[0]


def patch_mp4_artist(before: bytes, artist: str, properties: Properties) -> bytes:
    """Return a fixed-width candidate without opening or modifying a file."""
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
    structure, old_artist, _media_offset = _mp4_structure(before)
    verify_mp4(before, before, old_artist, properties, properties)
    position, size = _artist_atom(structure)
    # artist item header + data header + type/locale words = 24 bytes.
    offset = position + 24
    if len(encoded) != size - 24:
        raise Unsupported("artist byte length must remain unchanged")
    out = bytearray(before)
    out[offset : offset + len(encoded)] = encoded
    result = bytes(out)
    verify_mp4(before, result, artist, properties, properties)
    return result
