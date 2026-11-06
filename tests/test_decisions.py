"""Offline tests for the SystemOne decision-client abstraction.

Covers the provider answer-shape normalization (the reason the module
exists) and the client's transport behavior. All HTTP is mocked via the
shared :func:`mock_http` fixture; no network is touched.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from suno_dl.decisions import DecisionClient, DecisionError, _answer_to_result
from suno_dl.recommend import recommend_profiles


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        # TypeSafe Jev / local JEV fleet: explicit label + confidence.
        ({"label": "big", "confidence": 0.9}, ("big", 0.9)),
        ({"label": "big"}, ("big", 0.5)),
        # Needle: grounded label + uncalibrated head score under
        # 'raw_confidence'; the service ships no 'confidence' at all.
        (
            {
                "label": "light",
                "raw_confidence": 0.97,
                "confidence_semantics": "needle_head_score_uncalibrated",
            },
            ("light", 0.97),
        ),
        # Self-hosted Laya: winner under 'choice', per-option probabilities;
        # confidence is the max of the global value and the winner's share.
        (
            {"choice": "a", "probabilities": {"a": 0.94, "b": 0.06}, "confidence": 0.667},
            ("a", 0.94),
        ),
        # Probabilities without an explicit winner: argmax decides.
        ({"probabilities": {"a": 0.3, "b": 0.7}}, ("b", 0.7)),
        # Noul binary questions: the number is the yes-probability.
        ({"noul": 0.78}, ("yes", 0.78)),
    ],
)
def test_answer_to_result_normalizes_provider_shapes(
    node: dict, expected: tuple[str, float]
) -> None:
    result = _answer_to_result(node)
    assert result is not None
    assert (result.label, result.confidence) == expected


@pytest.mark.parametrize(
    "node",
    [
        {},  # nothing usable
        {"choice": ""},  # empty choice
        {"probabilities": {}},  # empty probabilities
        {"noul": "high"},  # non-numeric noul
    ],
)
def test_answer_to_result_rejects_unusable_nodes(node: dict) -> None:
    assert _answer_to_result(node) is None


def test_raw_confidence_wins_over_confidence_and_falls_back_to_default() -> None:
    # A grounded Needle node carries only 'raw_confidence'. Reading the
    # 'confidence' key alone pinned every grounded answer to the 0.5 default,
    # which silently compared as a constant against other providers' real
    # scores. 'raw_confidence' wins; 'confidence' stays a working fallback.
    needle = _answer_to_result({"label": "light", "raw_confidence": 0.97})
    assert needle is not None
    assert (needle.label, needle.confidence) == ("light", 0.97)

    legacy = _answer_to_result({"label": "big", "confidence": 0.9})
    assert legacy is not None
    assert (legacy.label, legacy.confidence) == ("big", 0.9)

    both = _answer_to_result({"label": "light", "raw_confidence": 0.97, "confidence": 0.1})
    assert both is not None
    assert both.confidence == 0.97

    neither = _answer_to_result({"label": "light"})
    assert neither is not None
    assert neither.confidence == 0.5

    # A Needle rejection nulls both the label and the score; it is not a
    # decision, so it must fall through to 'unusable', not to a 0.5 guess.
    rejected = _answer_to_result(
        {
            "label": None,
            "raw_confidence": None,
            "confidence_semantics": "ungrounded_rejection",
            "error": "Needle returned no grounded allowed choice",
        }
    )
    assert rejected is None


def test_client_builds_systemone_payload_and_sends_bearer(mock_http) -> None:  # type: ignore[no-untyped-def]
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {"pick": {"label": "beta", "confidence": 0.8}}})

    mock_http(handler)
    client = DecisionClient(
        "http://provider.test/v1/systemone", api_key="secret-token", model="jev-latest"
    )
    result = client.ask("the state", "Which one?", ["alpha", "beta"])

    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["state"] == "the state"
    assert seen["body"]["questions"]["pick"]["type"] == "choice"
    assert set(seen["body"]["questions"]["pick"]["criteria"]) == {"alpha", "beta"}
    assert (result.label, result.confidence) == ("beta", 0.8)


def test_client_rejects_single_option_questions() -> None:
    client = DecisionClient("http://provider.test/v1/systemone")
    with pytest.raises(DecisionError, match="at least two options"):
        client.ask("state", "Which?", ["only"])


def test_client_wraps_transport_failures(mock_http) -> None:  # type: ignore[no-untyped-def]
    mock_http(lambda request: httpx.Response(503, json={"error": "down"}))
    client = DecisionClient("http://provider.test/v1/systemone")
    with pytest.raises(DecisionError, match="HTTPStatusError"):
        client.ask("state", "Which?", ["a", "b"])


def test_client_rejects_winner_outside_options(mock_http) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"pick": {"label": "mystery-option"}}})

    mock_http(handler)
    client = DecisionClient("http://provider.test/v1/systemone")
    with pytest.raises(DecisionError, match="not among options"):
        client.ask("state", "Which?", ["a", "b"])


def test_client_surfaces_malformed_json_as_decision_error(mock_http) -> None:  # type: ignore[no-untyped-def]
    mock_http(lambda request: httpx.Response(200, text="not json"))
    client = DecisionClient("http://provider.test/v1/systemone")
    with pytest.raises(DecisionError):
        client.ask("state", "Which?", ["a", "b"])


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        "temporarily unavailable",
        {"answers": []},
        {"answers": {"pick": {"label": "a", "confidence": "unknown"}}},
        {"answers": {"pick": {"label": "a", "confidence": "NaN"}}},
        {"answers": {"pick": {"label": "a", "confidence": "Infinity"}}},
        {"answers": {"pick": {"probabilities": {"a": "unknown", "b": 0.2}}}},
        {"answers": {"pick": {"choice": "a", "probabilities": ["a", "b"]}}},
    ],
)
def test_client_malformed_provider_data_is_a_decision_error(mock_http, malformed) -> None:  # type: ignore[no-untyped-def]
    """A valid JSON response can still be unusable; never leak an unexpected error."""
    mock_http(lambda request: httpx.Response(200, json=malformed))
    client = DecisionClient("http://provider.test/v1/systemone")
    with pytest.raises(DecisionError):
        client.ask("state", "Which?", ["a", "b"])


def test_recommender_falls_back_on_invalid_answer_envelope(
    mock_http,
) -> None:  # type: ignore[no-untyped-def]
    """A bad live model reply must not crash an otherwise usable recommender."""
    mock_http(lambda request: httpx.Response(200, json={"answers": []}))

    async def fetch(_url: str) -> str:
        return '{"audios":[{"title":"One song"}],"followers_count":7}'

    recommendations = asyncio.run(
        recommend_profiles(
            ["alpha", "beta"],
            fetch_html=fetch,
            jev_url="http://provider.test/v1/evaluate",
        )
    )
    assert len(recommendations) == 2
    assert all(recommendation.engine == "heuristic" for recommendation in recommendations)


def test_transport_error_never_echoes_endpoint_credentials(mock_http) -> None:  # type: ignore[no-untyped-def]
    mock_http(lambda request: httpx.Response(503, json={"error": "down"}))
    url = "https://user:synthetic-password@jev.example/v1/systemone?token=synthetic-token#frag"
    client = DecisionClient(url)
    with pytest.raises(DecisionError) as excinfo:
        client.ask("state", "Which?", ["a", "b"])
    message = str(excinfo.value)
    assert "HTTPStatusError" in message
    assert "synthetic-password" not in message
    assert "synthetic-token" not in message
    assert "jev.example" not in message
