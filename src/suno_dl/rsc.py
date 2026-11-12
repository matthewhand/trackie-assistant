"""Bounded structural reads of public React Flight data; text is never data."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate React Flight JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-JSON React Flight number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite React Flight number")
    return result


def _decode(value):
    return json.loads(
        value, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
    )


def flight_documents(html: str) -> Iterator[Any]:
    if len(html.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("React Flight HTML exceeds the size limit")
    fragments: list[str] = []
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        text = script.string or ""
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)\s*;?", text, re.DOTALL)
        if not match:
            continue
        try:
            frame = _decode(match.group(1))
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid React Flight frame") from exc
        if (
            isinstance(frame, list)
            and len(frame) == 2
            and frame[0] == 1
            and isinstance(frame[1], str)
        ):
            fragments.append(frame[1])
    # Flight's length-prefixed text records can contain arbitrary lyrics,
    # including fake JSON record lines. Skip them by byte length, never by
    # newline or by searching for a profile-shaped object in untrusted text.
    stream = "".join(fragments).encode("utf-8")
    offset = 0
    while offset < len(stream):
        if stream[offset : offset + 1] == b"\n":
            offset += 1
            continue
        record = re.match(rb"[0-9a-f]*:", stream[offset:])
        if record is None:
            raise ValueError("unsupported React Flight framing")
        offset += record.end()
        if stream[offset : offset + 1] == b"T":
            length = re.match(rb"T([0-9a-f]{1,8}),", stream[offset:])
            if length is None:
                raise ValueError("invalid React Flight text framing")
            offset += length.end()
            offset += int(length.group(1), 16)
            if offset > len(stream):
                raise ValueError("incomplete React Flight text frame")
            continue
        end = stream.find(b"\n", offset)
        if end == -1:
            end = len(stream)
        value = stream[offset:end]
        offset = end + 1
        if not value.startswith((b"[", b"{")):
            continue
        try:
            tree = _decode(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid React Flight data") from exc
        yield tree
