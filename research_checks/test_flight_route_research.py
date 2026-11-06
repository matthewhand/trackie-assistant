"""Fixture-specific route experiment. Results are *not* producer evidence.

The captured page embeds a route and a client element with a clip prop. Its
client module implementation and Suno's producer-field contract are absent.
These tests exercise one observed structural shape only; a matching shape does
not establish that the prop is rendered or that its user fields mean creator.
Nothing here is imported into production or relaxes the attribution guard.
"""

import re
from copy import deepcopy
from uuid import UUID

import pytest

from research_checks.test_flight_framing_model import _captured_stream
from tests.flight_framing_model import Row, parse_flight_bytes


class UnsupportedRoute(ValueError):
    pass


def _observed_clip(rows: tuple[Row, ...], expected_id: str) -> dict:
    """Identify the observed fixture path, making no producer assertion.

    This is intentionally rigid, fail-closed fixture analysis, not a general
    React/Next parser or a suitable extractor for live Suno pages.
    """
    try:
        if str(UUID(expected_id)) != expected_id:
            raise UnsupportedRoute("noncanonical UUID")
        index = {row.row_id: row for row in rows if row.row_id is not None}

        def follow(value, kind):
            if not isinstance(value, str) or not re.fullmatch(r"\$L(?:0|[1-9a-f][0-9a-f]*)", value):
                raise UnsupportedRoute("unsupported reference")
            target = index[int(value[2:], 16)]
            if target.kind != kind:
                raise UnsupportedRoute("wrong referenced row kind")
            return target.value

        root = index[0]
        if root.kind != "model" or root.value["c"] != ["", "song", expected_id]:
            raise UnsupportedRoute("route mismatch")
        flight = root.value["f"]
        if len(flight) != 1:
            raise UnsupportedRoute("unsupported route count")
        route = flight[0][0]
        if route[0] != "":
            raise UnsupportedRoute("unsupported root")
        route = route[1]["children"]
        if route[0] != "(root)":
            raise UnsupportedRoute("unsupported root segment")
        route = route[1]["children"]
        if route[0] != "song":
            raise UnsupportedRoute("unsupported song segment")
        route = route[1]["children"]
        if route[0] != ["slug", expected_id, "d", []]:
            raise UnsupportedRoute("slug mismatch")
        if route[1]["children"][0] != "__PAGE__":
            raise UnsupportedRoute("unsupported page")

        element = flight[0][1][1]
        for _ in range(3):
            element = element["children"][1]
        page_ref = element["children"][0]
        page = follow(page_ref, "model")
        if page[:3] != ["$", "$1", "c"]:
            raise UnsupportedRoute("unexpected page element")
        candidate = follow(page[3]["children"][0], "model")
        if not isinstance(candidate, list) or len(candidate) != 4 or candidate[0] != "$":
            raise UnsupportedRoute("unexpected clip element")
        follow(candidate[1], "module")
        clip = candidate[3]["clip"]
        if not isinstance(clip, dict) or clip.get("id") != expected_id:
            raise UnsupportedRoute("clip mismatch")
        return clip
    except UnsupportedRoute:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise UnsupportedRoute("unsupported fixture shape") from exc


def _replace(rows, row_id, value):
    return tuple(Row(row.row_id, row.kind, value) if row.row_id == row_id else row for row in rows)


def _fixture():
    rows = parse_flight_bytes(_captured_stream())
    root = next(row for row in rows if row.row_id == 0 and row.kind == "model")
    return rows, root.value["c"][2]


def test_captured_route_reaches_one_clip_prop_without_proving_creator():
    rows, clip_id = _fixture()
    candidate = _observed_clip(rows, clip_id)
    assert candidate["id"] == clip_id
    assert candidate["entity_type"] == "song_schema"
    assert candidate["display_name"] == candidate["handle"] == "drinko"


def test_unrelated_clip_decoy_does_not_change_observed_path():
    rows, clip_id = _fixture()
    root = deepcopy(next(row.value for row in rows if row.row_id == 0))
    root["untrusted_comments"] = {"clip": {"id": clip_id, "display_name": "forged"}}
    rows = _replace(rows, 0, root)
    assert _observed_clip(rows, clip_id)["display_name"] == "drinko"


@pytest.mark.parametrize("location", ["root", "route", "clip", "component"])
def test_mismatch_or_unsupported_reference_is_rejected(location):
    rows, clip_id = _fixture()
    root = deepcopy(next(row.value for row in rows if row.row_id == 0))
    page = deepcopy(next(row.value for row in rows if row.row_id == 0x40))
    if location == "root":
        root["c"][2] = "11111111-1111-4111-8111-111111111111"
    elif location == "route":
        root["f"][0][0][1]["children"][1]["children"][1]["children"][0][1] = (
            "11111111-1111-4111-8111-111111111111"
        )
    elif location == "clip":
        page[3]["clip"]["id"] = "11111111-1111-4111-8111-111111111111"
    else:
        page[1] = "$L51"  # text row rather than client module
    rows = _replace(_replace(rows, 0, root), 0x40, page)
    with pytest.raises(UnsupportedRoute):
        _observed_clip(rows, clip_id)


def test_matching_route_still_cannot_validate_creator_field_meaning():
    rows, clip_id = _fixture()
    candidate_element = deepcopy(next(row.value for row in rows if row.row_id == 0x40))
    candidate_element[3]["clip"]["display_name"] = "Unproven replacement"
    altered = _replace(rows, 0x40, candidate_element)
    # Both inputs fit the same page/element structure. Without the missing
    # component/schema contract there is no basis to call either name verified.
    assert _observed_clip(rows, clip_id)["display_name"] == "drinko"
    assert _observed_clip(altered, clip_id)["display_name"] == "Unproven replacement"


def test_coherent_identity_fields_and_clip_id_still_do_not_prove_attribution():
    rows, clip_id = _fixture()
    candidate = deepcopy(next(row.value for row in rows if row.row_id == 0x40))
    candidate[3]["clip"].update(
        display_name="Unverified Account",
        handle="unverified_account",
        user_id="22222222-2222-4222-8222-222222222222",
    )
    altered = _replace(rows, 0x40, candidate)
    observed = _observed_clip(altered, clip_id)
    assert observed["id"] == clip_id
    assert observed["entity_type"] == "song_schema"
    assert observed["display_name"] == "Unverified Account"
    assert observed["handle"] == "unverified_account"
    # A coherent-looking tuple can be placed in the same component prop. Its
    # meaning remains unknown without the component and producer contract.
