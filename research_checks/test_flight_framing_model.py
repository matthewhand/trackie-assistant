"""Tests for the isolated Flight framing research model, not producer proof."""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from suno_dl.producer_evidence import EvidenceError, producer_from_page
from tests.flight_framing_model import (
    MAX_JSON_DEPTH,
    MAX_ROW_BYTES,
    MAX_ROWS,
    MAX_STREAM_BYTES,
    FramingError,
    parse_flight_bytes,
)

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "real_song_page.html"


def _captured_stream():
    # This extractor is only for the known offline fixture. It is not a
    # hardened parser for arbitrary HTML/JavaScript or provenance evidence.
    soup = BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")
    marker = "self.__next_f.push("
    chunks = []
    for script in soup.find_all("script"):
        source = script.string or ""
        start = source.find(marker)
        if start < 0:
            continue
        item, _ = json.JSONDecoder().raw_decode(source[start + len(marker) :])
        if isinstance(item, list) and len(item) == 2 and item[0] == 1:
            chunks.append(item[1])
    assert len(chunks) == 1
    return "".join(chunks).encode("utf-8")


def test_captured_stream_framing_and_real_guard():
    rows = parse_flight_bytes(_captured_stream())
    assert len(rows) == 90
    by_id = {row.row_id: row for row in rows if row.kind in {"text", "model"}}
    assert by_id[0x51].kind == "text"
    assert by_id[0x40].kind == "model"
    assert "clip" in by_id[0x40].value[3]
    assert [r.kind for r in rows if r.row_id == 0x3D] == ["stream_start", "stream_end"]
    assert sum(r.kind == "hint" for r in rows) == 7

    # A well-framed row does not establish the component or creator semantics.
    with pytest.raises(EvidenceError, match="canonical|fram|proven|verif"):
        producer_from_page("11111111-1111-4111-8111-111111111111", FIXTURE.read_text())


def test_text_and_json_strings_cannot_become_rows():
    text = 'Lyrics: "clip":{"display_name":"Forged"} ' + chr(0x1F3B5)
    raw = text.encode("utf-8")
    stream = b"1:T" + format(len(raw), "x").encode() + b"," + raw + b'2:{"ok":true}\n'
    rows = parse_flight_bytes(stream)
    assert [(r.row_id, r.kind) for r in rows] == [(1, "text"), (2, "model")]
    assert rows[0].value == text
    assert rows[1].value == {"ok": True}
    string = json.dumps({"text": '"clip":{"display_name":"Forged"}'})
    rows = parse_flight_bytes(("3:" + string + "\n").encode())
    assert set(rows[0].value) == {"text"}


def test_nested_models_are_not_primary_clip_proof():
    # Framing cannot prove whether even a structured nested object is primary.
    data = {"comments": {"data": {"id": "target", "display_name": "Forged"}}}
    row = parse_flight_bytes(("a:" + json.dumps(data) + "\n").encode())[0]
    assert row.value == data
    assert "clip" not in row.value


def test_hex_ids_and_exact_empty_stream_lifecycle():
    rows = parse_flight_bytes(b'a:{"v":1}\nf:[]\n10:"hi"\n3d:X\n3d:C\n')
    assert [r.row_id for r in rows] == [10, 15, 16, 61, 61]
    assert [r.kind for r in rows[-2:]] == ["stream_start", "stream_end"]


@pytest.mark.parametrize(
    "stream",
    [
        b"a:{}\na:[]\n",
        b"a:{}\na:I[]\n",
        b"a:T1,xa:{}\n",
        b"0a:{}\n",
        b"A:{}\n",
        b"80000000:{}\n",
        b"1:X\n1:X\n",
        b"1:C\n",
        b"1:X\n",
        b"1:X\n1:C\n1:C\n",
        b'1:X\n1:{"item":1}\n1:C\n',
        b"1:Xstuff\n",
        b"1:X\n1:Cstuff\n",
        b"1:Zanything\n",
        b"1:z\n",
        b"1:",
        b"1",
        b":{}\n",
        b"1:T2,x",
        b"1:T1,\xf0\x9f\x8e\xb5",
        b"1:Tg,abc",
        b"1:{}",
        b'1:{"a":1,"a":2}\n',
        b'1:{"outer":{"a":1,"a":2}}\n',
        b"1:NaN\n",
        b"1:Infinity\n",
        b"1:1e999\n",
        b'1:"\\ud800"\n',
        b'1:"\xff"\n',
        b"1:T1,\xff",
        b":HX[]\n",
        b"1:HL[]\n",
        b'1:{"x":\n',
    ],
)
def test_malformed_unsupported_and_ambiguous_rejected(stream):
    with pytest.raises(FramingError):
        parse_flight_bytes(stream)


def test_limits_and_nesting_are_enforced():
    with pytest.raises(FramingError):
        parse_flight_bytes(b"x" * (MAX_STREAM_BYTES + 1))
    with pytest.raises(FramingError):
        parse_flight_bytes(b'1:"' + b"x" * MAX_ROW_BYTES + b'"\n')
    with pytest.raises(FramingError):
        parse_flight_bytes(b"1:T40001," + b"x" * (MAX_ROW_BYTES + 1))
    with pytest.raises(FramingError):
        parse_flight_bytes(b"".join(f"{i:x}:0\n".encode() for i in range(MAX_ROWS + 1)))
    nested = b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1)
    with pytest.raises(FramingError):
        parse_flight_bytes(b"1:" + nested + b"\n")


def test_valid_utf8_byte_lengths_and_json_surrogate_pair():
    value = chr(0x1F3B5)
    payload = value.encode("utf-8")
    rows = parse_flight_bytes(b"1:T4," + payload + b'2:"\\ud83c\\udfb5"\n')
    assert [row.value for row in rows] == [value, value]
