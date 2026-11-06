"""Tests for bounded, offline MP4 sample-table research only."""

from pathlib import Path

import pytest

from tests.artist_binary_proof_model import Unsupported
from tests.mp4_sample_table_model import inspect_sample_tables


@pytest.fixture
def fixture():
    return (Path(__file__).parents[1] / "tests" / "fixtures" / "tiny.m4a").read_bytes()


def _replace_word(data, marker, offset, value):
    out = bytearray(data)
    pos = data.index(marker) + offset
    out[pos : pos + 4] = value.to_bytes(4, "big")
    return bytes(out)


def test_real_fixture_has_coherent_sample_tables(fixture):
    assert inspect_sample_tables(fixture) == (23, 1)


@pytest.mark.parametrize(
    ("marker", "offset", "value"),
    [
        (b"stco", 12, 1),  # points before mdat payload
        (b"stco", 12, 0xFFFFFFFF),  # outside mdat
        (b"stsz", 12, 999),  # wrong sample count
        (b"stsz", 16, 0),  # zero-size sample
        (b"stts", 12, 21),  # count does not match samples
        (b"sbgp", 16, 22),  # grouping does not cover all samples
        (b"stsc", 12, 2),  # first mapping must start at chunk 1
        (b"stsc", 20, 2),  # unsupported sample description
    ],
)
def test_inconsistent_sample_tables_are_rejected(fixture, marker, offset, value):
    with pytest.raises(Unsupported):
        inspect_sample_tables(_replace_word(fixture, marker, offset, value))


def test_external_reference_is_rejected(fixture):
    data = bytearray(fixture)
    data[data.index(b"url ") + 7] = 0
    with pytest.raises(Unsupported):
        inspect_sample_tables(bytes(data))
