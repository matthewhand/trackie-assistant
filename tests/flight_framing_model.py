"""Restricted offline Flight framing research model, never producer evidence.

Reference consulted: React v19.2.0 ReactFlightClient.js, processBinaryChunk
and processFullStringRow:
https://github.com/facebook/react/blob/v19.2.0/packages/react-client/src/ReactFlightClient.js

The captured Suno page's React version is unknown. This is a deliberately
narrow subset derived from that one fixture, NOT a compatible React decoder.
It accepts model JSON, module JSON, text, a limited hint and an empty async
stream start/stop pair. Unknown tags, content-bearing streams, malformed
framing and unsupported JSON fail closed. No HTML extraction, reference
resolution, component semantics, producer identity, or provenance is provided.
Production guards MUST remain in place independently of this model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

MAX_STREAM_BYTES = 1024 * 1024
MAX_ROW_BYTES = 256 * 1024
MAX_ROWS = 4096
MAX_JSON_DEPTH = 64


class FramingError(ValueError):
    """Framing is malformed, unsupported, ambiguous or over the budget."""


@dataclass(frozen=True)
class Row:
    """One fully framed row; JSON strings remain strings, references unresolved."""

    row_id: int | None
    kind: str
    value: Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FramingError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise FramingError("nonfinite JSON number")
    return value


def _constant(_value: str) -> None:
    raise FramingError("nonfinite JSON constant")


def _validate_json(value: Any) -> None:
    # Iterative inspection bounds traversal even for adversarial nesting.
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise FramingError("JSON nesting exceeds limit")
        if isinstance(current, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in current):
                raise FramingError("unpaired surrogate in JSON")
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, dict):
            for key, item in current.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))


def _json_payload(payload: bytes) -> Any:
    try:
        value = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_constant,
        )
        _validate_json(value)
        return value
    except FramingError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise FramingError("invalid JSON row") from exc


def _hex(raw: bytes, *, allow_empty: bool = False) -> int | None:
    if not raw and allow_empty:
        return None
    if (
        not raw
        or len(raw) > 8
        or any(char not in b"0123456789abcdef" for char in raw)
        or (len(raw) > 1 and raw[0] == 48)
    ):
        raise FramingError("invalid or noncanonical hexadecimal field")
    result = int(raw, 16)
    if result > 0x7FFFFFFF:
        raise FramingError("hexadecimal field exceeds model limit")
    return result


def parse_flight_bytes(data: bytes) -> tuple[Row, ...]:
    """Parse a complete bounded byte stream; never follow or interpret refs."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_STREAM_BYTES:
        raise FramingError("invalid or oversized stream")
    rows: list[Row] = []
    # This is not a general streaming implementation: only the captured
    # empty X->C lifecycle is understood, never data-bearing stream events.
    addresses: dict[int, str] = {}
    cursor = 0
    while cursor < len(data):
        if len(rows) >= MAX_ROWS:
            raise FramingError("too many rows")
        colon = data.find(b":", cursor, min(len(data), cursor + 10))
        if colon < 0:
            raise FramingError("missing or oversized row ID")
        row_id = _hex(data[cursor:colon], allow_empty=True)
        cursor = colon + 1
        if cursor == len(data):
            raise FramingError("truncated row tag")
        tag = chr(data[cursor]) if 65 <= data[cursor] <= 90 else ""
        if tag:
            cursor += 1
        if tag == "T":
            comma = data.find(b",", cursor, min(len(data), cursor + 10))
            if comma < 0:
                raise FramingError("missing or oversized text length")
            length = _hex(data[cursor:comma])
            if length is None or length > MAX_ROW_BYTES:
                raise FramingError("text row too large")
            cursor = comma + 1
            end = cursor + length
            if end > len(data):
                raise FramingError("truncated text row")
            payload = data[cursor:end]
            cursor = end
        else:
            end = data.find(b"\n", cursor)
            if end < 0:
                raise FramingError("unterminated row")
            if end - cursor > MAX_ROW_BYTES:
                raise FramingError("row too large")
            payload = data[cursor:end]
            cursor = end + 1

        if tag == "H":
            # Hints do not define evidence-bearing IDs. The observed fixture
            # uses :HL[...] only; other hint shapes are outside this model.
            if row_id is not None or not payload.startswith(b"L"):
                raise FramingError("unsupported hint")
            rows.append(Row(None, "hint", _json_payload(payload[1:])))
            continue
        if row_id is None:
            raise FramingError("missing addressable row ID")
        if tag == "X":
            if payload or row_id in addresses:
                raise FramingError("unsupported or duplicate stream start")
            addresses[row_id] = "stream_open"
            rows.append(Row(row_id, "stream_start", None))
            continue
        if tag == "C":
            if payload or addresses.get(row_id) != "stream_open":
                raise FramingError("unsupported stream close")
            addresses[row_id] = "stream_closed"
            rows.append(Row(row_id, "stream_end", None))
            continue
        if row_id in addresses:
            raise FramingError("duplicate or unsupported stream row")
        if tag == "T":
            try:
                value = payload.decode("utf-8", "strict")
            except UnicodeError as exc:
                raise FramingError("invalid text UTF-8") from exc
            kind = "text"
        elif tag in ("", "I"):
            value = _json_payload(payload)
            kind = "model" if not tag else "module"
        else:
            raise FramingError("unsupported row tag")
        addresses[row_id] = kind
        rows.append(Row(row_id, kind, value))
    if "stream_open" in addresses.values():
        raise FramingError("unclosed stream")
    return tuple(rows)
