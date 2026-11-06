"""Test-only bounded MP4 artist growth into an adjacent zero-filled free box.

This is not a production writer or safety proof. It does not establish the
meaning of unknown metadata, references, checksums or signatures; it does not
validate audio decoding, establish producer provenance, check actual file
attributes, coordinate writers or publish files. Keep production guard closed.
"""

from __future__ import annotations

import unicodedata

from tests.artist_binary_proof_model import (
    Properties,
    Unsupported,
    _mp4_structure,
    _properties_match,
)

_PATH = (b"moov", b"udta", b"meta", b"ilst")


def _only(entries: tuple, name: bytes) -> tuple:
    matches = [entry for entry in entries if entry[2][0] == name]
    if len(matches) != 1:
        raise Unsupported("ambiguous or missing container")
    return matches[0]


def patch_mp4_artist_using_padding(before: bytes, artist: str, props: Properties) -> bytes:
    """Return a candidate only for the explicit free-padding fixture shape."""
    if (
        not isinstance(artist, str)
        or not artist.strip()
        or len(artist) > 256
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in artist)
    ):
        raise Unsupported("invalid artist")
    encoded = artist.encode("utf-8", "strict")
    if len(encoded) > 512:
        raise Unsupported("artist exceeds byte limit")
    _properties_match(props, props)
    structure, _old_artist, media_pos = _mp4_structure(before)
    children = structure
    for name in _PATH[:-1]:
        container = _only(children, name)
        children = container[2][-1]
        if not isinstance(children, tuple):
            raise Unsupported("invalid container children")
    ilst = _only(children, _PATH[-1])
    ilst_pos, ilst_size, ilst_item = ilst
    meta_children = children
    items = ilst_item[-1]
    if not items or items[-1][2] != (b"\xa9ART", "artist"):
        raise Unsupported("artist must be the final item")
    artist_pos, artist_size, _item = items[-1]
    index = meta_children.index(ilst)
    if index + 1 >= len(meta_children):
        raise Unsupported("missing adjacent free padding")
    free_pos, free_size, free_item = meta_children[index + 1]
    if (
        free_item[0] != b"free"
        or free_pos != ilst_pos + ilst_size
        or artist_pos + artist_size != free_pos
        or free_item[1] != before[free_pos : free_pos + free_size]
        or any(before[free_pos + 8 : free_pos + free_size])
    ):
        raise Unsupported("padding is not an adjacent zero-filled free box")
    new_size = len(encoded) + 24
    delta = new_size - artist_size
    new_free_size = free_size - delta
    if new_free_size < 8 or ilst_size + delta < 8:
        raise Unsupported("insufficient free padding")
    header = bytearray(before[artist_pos : artist_pos + 24])
    header[:4] = new_size.to_bytes(4, "big")
    header[8:12] = (new_size - 8).to_bytes(4, "big")
    free = new_free_size.to_bytes(4, "big") + b"free" + bytes(new_free_size - 8)
    out = bytearray(
        before[:artist_pos] + bytes(header) + encoded + free + before[free_pos + free_size :]
    )
    out[ilst_pos : ilst_pos + 4] = (ilst_size + delta).to_bytes(4, "big")
    result = bytes(out)
    _new_structure, observed, new_media_pos = _mp4_structure(result)
    if (
        len(result) != len(before)
        or observed != artist
        or media_pos != new_media_pos
        or result[:ilst_pos] != before[:ilst_pos]
        or result[ilst_pos + 4 : artist_pos] != before[ilst_pos + 4 : artist_pos]
        or result[free_pos + free_size :] != before[free_pos + free_size :]
    ):
        raise Unsupported("non-artist data changed")
    return result
