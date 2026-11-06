"""Disposable-fixture tests for the unintegrated MP4 sample-entry experiment."""

from pathlib import Path

import pytest

from tests.artist_binary_proof_model import Unsupported
from tests.mp4_sample_entry_model import inspect_data_reference, inspect_sample_description


def _fixture_atom(name: bytes) -> bytes:
    data = (Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a").read_bytes()
    offset = data.index(name)
    size = int.from_bytes(data[offset - 4 : offset], "big")
    return data[offset - 4 : offset - 4 + size]


def test_disposable_real_fixture_has_inspectable_entries():
    inspect_sample_description(_fixture_atom(b"stsd"))
    inspect_data_reference(_fixture_atom(b"dref"))


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (3, 0),  # invalid box length
        (4, ord("x")),  # incorrect outer type
        (11, 1),  # unsupported outer flags
        (15, 2),  # multiple entries
        (20, ord("x")),  # unsupported codec entry
        (31, 2),  # external data-reference index
        (33, 1),  # nonzero sample-entry version
        (65, 1),  # ESDS flags
        (66, 1),  # ESDS flags
        (67, 1),  # ESDS flags
    ],
)
def test_rejects_modified_sample_entry(index, value):
    data = bytearray(_fixture_atom(b"stsd"))
    data[index] = value
    with pytest.raises(Unsupported):
        inspect_sample_description(bytes(data))


@pytest.mark.parametrize(("index", "value"), [(3, 0), (11, 1), (15, 2), (20, ord("x")), (27, 0)])
def test_rejects_unsupported_or_external_data_reference(index, value):
    data = bytearray(_fixture_atom(b"dref"))
    data[index] = value
    with pytest.raises(Unsupported):
        inspect_data_reference(bytes(data))


def test_sample_entry_structure_is_not_an_aac_integrity_proof():
    # Keep all descriptor sizes and types but replace the decoder-specific AAC
    # configuration with arbitrary bytes. Structural inspection is not decode
    # verification and must not be used to authorize writes.
    data = bytearray(_fixture_atom(b"stsd"))
    marker = bytes.fromhex("0580808005120856e500")
    start = data.index(marker) + 5
    data[start : start + 5] = bytes(5)
    inspect_sample_description(bytes(data))
