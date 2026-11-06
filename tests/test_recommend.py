"""Offline tests for the JEV-backed profile recommender.

No network is touched: candidate pages are canned HTML strings, the follow
lists and the JEV decision service are httpx MockTransport handlers that
assert the request shape before answering.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from suno_dl.recommend import (
    Recommendation,
    _expand_follow_list,
    _follow_list_url,
    _parse_follow_list,
    _parse_profile_signals,
    _profile_url,
    _taste_titles_from_archive,
    recommend_cli,
    recommend_profiles,
)


def _profile_html(tracks: list[str], followers: int = 12) -> str:
    audios = ",".join(json.dumps({"title": t}) for t in tracks)
    decoys = json.dumps({"title": "Followers"})
    return json.dumps({"followers_count": followers})[:-1] + f',"audios":[{audios},{decoys}]}}'


# ─────────────────────────── signal extraction ───────────────────────────


def test_parse_profile_signals_extracts_tracks_and_followers() -> None:
    html = _profile_html(["Song A", "Song B", "Song A"], followers=34)
    tracks, followers = _parse_profile_signals(html)
    assert [t.title for t in tracks] == ["Song A", "Song B"]  # deduped
    assert followers == 34


def test_parse_profile_signals_handles_missing_followers() -> None:
    tracks, followers = _parse_profile_signals('{"audios":[{"title":"Only One"}]}')
    assert [t.title for t in tracks] == ["Only One"]
    assert followers is None


def test_parse_profile_signals_unescapes_unicode_entities() -> None:
    """Suno encodes & as \\u0026 inside titles — it must arrive decoded."""
    html = '{"audios":[{"title":"Oath of Chandra (feat. Chandra \\u0026 Max Stirner)"}]}'
    tracks, _ = _parse_profile_signals(html)
    assert tracks[0].title == "Oath of Chandra (feat. Chandra & Max Stirner)"


def test_profile_url_accepts_handle_at_and_full_url() -> None:
    assert _profile_url("drinko") == ("https://suno.com/@drinko", "drinko")
    assert _profile_url("@other_user") == ("https://suno.com/@other_user", "other_user")
    url, handle = _profile_url("https://suno.com/@third/")
    assert handle == "third" and url.endswith("/@third")


def test_profile_url_rejects_non_profiles() -> None:
    import click

    with pytest.raises(click.ClickException):
        _profile_url("https://example.com/@nope")


# ─────────────────────────── JEV-backed ranking ───────────────────────────


@pytest.mark.parametrize(
    "jev_url",
    [
        "http://jev.test/v1/evaluate",
        "https://user:SYNTHETIC_SECRET@jev.test/v1/evaluate?token=SYNTHETIC_TOKEN#private",
    ],
)
def test_recommend_profiles_ranks_via_jev(monkeypatch: pytest.MonkeyPatch, jev_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            handle = str(request.url).rstrip("/").split("/@")[-1]
            pages = {
                "big": _profile_html(["T1", "T2", "T3", "T4", "T5"], followers=100),
                "small": _profile_html(["S1"], followers=2),
            }
            return httpx.Response(200, text=pages[handle])
        # JEV service: assert the SystemOne question shape, then pick "big".
        body = json.loads(request.content)
        assert body["questions"]["pick"]["type"] == "choice"
        assert set(body["questions"]["pick"]["criteria"]) == {"big", "small"}
        return httpx.Response(
            200,
            json={"answers": {"pick": {"type": "choice", "label": "big", "confidence": 0.9}}},
        )

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)

    result = asyncio.run(recommend_profiles(["big", "small"], jev_url=jev_url))
    assert [r.handle for r in result] == ["big", "small"]
    assert result[0].score > result[1].score
    assert result[0].engine.startswith("jev:")
    assert "SYNTHETIC_SECRET" not in result[0].engine
    assert "SYNTHETIC_TOKEN" not in result[0].engine
    assert "jev.test" not in result[0].engine
    assert result[0].track_count == 5
    assert result[0].followers == 100
    assert isinstance(result[0], Recommendation)


def test_recommend_profiles_falls_back_to_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            return httpx.Response(200, text=_profile_html(["T1", "T2"], followers=7))
        return httpx.Response(503, json={"error": "down"})

    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    result = asyncio.run(
        recommend_profiles(["alpha", "beta"], jev_url="http://jev.test/v1/evaluate")
    )
    assert len(result) == 2
    assert all(r.engine == "heuristic" for r in result)
    # Deterministic: equal signals → equal scores, order stable.
    assert result[0].score == result[1].score
    assert "2 public tracks" in result[0].rationale


def test_recommend_profiles_empty_candidates() -> None:
    assert asyncio.run(recommend_profiles([])) == []


def test_cli_does_not_send_a_model_id_unless_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import suno_dl.recommend as recommend

    monkeypatch.setattr(recommend, "ensure_dotenv_loaded", lambda: None)
    monkeypatch.setattr(recommend, "DEFAULT_JEV_MODEL", None)
    monkeypatch.delenv("SUNO_JEV_MODEL", raising=False)
    captured: list[str | None] = []

    async def fake_recommend_profiles(*_args: object, **kwargs: object) -> list[Recommendation]:
        model = kwargs.get("jev_model")
        captured.append(model if isinstance(model, str) else None)
        return [
            Recommendation(
                handle="alpha",
                track_count=1,
                followers=1,
                latest_title="Example",
                score=0.5,
                rationale="offline signal",
                engine="heuristic",
            )
        ]

    monkeypatch.setattr(recommend, "recommend_profiles", fake_recommend_profiles)

    runner = CliRunner()
    unconfigured = runner.invoke(recommend_cli, ["alpha"])
    assert unconfigured.exit_code == 0, unconfigured.output
    explicit = runner.invoke(recommend_cli, ["alpha", "--jev-model", "explicit-remote-model"])
    assert explicit.exit_code == 0, explicit.output
    assert captured == [None, "explicit-remote-model"]


def test_unconfigured_jev_never_connects_to_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable installs without SUNO_JEV_URL rank offline, without a JEV probe."""
    import suno_dl.recommend as recommend

    def forbidden_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no JEV connection should be attempted")

    monkeypatch.setattr(recommend, "DecisionClient", forbidden_client)

    async def fetch_profile(_url: str) -> str:
        return _profile_html(["One song"], followers=7)

    result = asyncio.run(
        recommend_profiles(["alpha", "beta"], fetch_html=fetch_profile, jev_url="")
    )
    assert len(result) == 2
    assert all(rec.engine == "heuristic" for rec in result)


def test_sync_fetcher_html_is_called_once_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import suno_dl.recommend as recommend

    monkeypatch.setattr(recommend, "_jev_rank_sync", lambda *_args, **_kwargs: None)
    fetched_urls: list[str] = []

    def fetch(url: str) -> str:
        fetched_urls.append(url)
        return _profile_html(["Real Track"], followers=12)

    result = asyncio.run(recommend_profiles(["alpha"], fetch_html=fetch))
    assert fetched_urls == ["https://suno.com/@alpha"]
    assert len(result) == 1
    assert result[0].handle == "alpha"
    assert result[0].track_count == 1
    assert result[0].followers == 12
    assert result[0].engine == "heuristic"


def test_sync_fetcher_preparsed_result_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import suno_dl.recommend as recommend

    monkeypatch.setattr(recommend, "_jev_rank_sync", lambda *_args, **_kwargs: None)

    def fetch(_url: str) -> tuple[list[recommend.SignalTrack], int]:
        return ([recommend.SignalTrack(title="Preparsed Track")], 3)

    result = asyncio.run(recommend_profiles(["alpha"], fetch_html=fetch))
    assert len(result) == 1
    assert result[0].latest_title == "Preparsed Track"
    assert result[0].followers == 3


def test_parse_profile_signals_handles_escaped_flight_payload() -> None:
    """Real Suno pages escape quotes in their Next.js flight payload."""
    html = (
        '{\\"modal\\":{\\"title\\":\\"Get the full app experience\\"},'
        '\\"stats\\":{\\"followers_count\\":34,\\"following_count\\":3},'
        '\\"audios\\":[{\\"title\\":\\"Drinko, drinko, drinko\\"},'
        '{\\"title\\":\\"Make Disappear [nsfw] (feat. Max Stirner)\\"},'
        '{\\"title\\":\\"Publish Song\\"}]}'
    )
    tracks, followers = _parse_profile_signals(html)
    titles = [t.title for t in tracks]
    assert "Drinko, drinko, drinko" in titles
    assert "Make Disappear [nsfw] (feat. Max Stirner)" in titles
    assert "Publish Song" not in titles  # UI chrome filtered
    assert followers == 34


# ─────────────────────── opt-in follow-list expansion ───────────────────


def test_follow_list_url_validates_handle_and_kind() -> None:
    import click

    assert _follow_list_url("drinko", "following") == (
        "https://suno.com/api/profiles/drinko/following"
    )
    assert _follow_list_url("@x.y", "followers").endswith("/api/profiles/x.y/followers")
    with pytest.raises(click.ClickException):
        _follow_list_url("bad handle!", "following")
    with pytest.raises(ValueError):
        _follow_list_url("drinko", "enemies")


def test_parse_follow_list_tolerates_shape_drift() -> None:
    assert _parse_follow_list(["a", "b", "@a"]) == ["a", "b"]  # dedupe + @-strip
    assert _parse_follow_list([{"username": "u1"}, {"handle": "u2"}, {"name": "u3"}]) == [
        "u1",
        "u2",
        "u3",
    ]
    assert _parse_follow_list({"profiles": [{"username": "w1"}]}) == ["w1"]
    assert _parse_follow_list({"following": ["w2"]}) == ["w2"]
    assert _parse_follow_list({"unexpected": ["w3"]}) == []
    assert _parse_follow_list("nope") == []  # type: ignore[arg-type]


def test_expand_follow_list_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Cookie" in request.headers  # session travels in headers only
        return httpx.Response(200, json={"profiles": [{"username": "friend1"}]})

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    handles = asyncio.run(_expand_follow_list("seed", "following", "session=secret-value"))
    assert handles == ["friend1"]

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    def patched_fail_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(failing_handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_fail_init)
    handles = asyncio.run(_expand_follow_list("seed", "followers", "session=secret-value"))
    assert handles == []  # graceful empty list, cookie never surfaced


def test_recommend_profiles_expands_follow_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "jev.test" in url:
            return httpx.Response(
                200,
                json={"answers": {"pick": {"label": "grower", "confidence": 0.8}}},
            )
        if "/api/profiles/seed/following" in url:
            assert "Cookie" in request.headers
            return httpx.Response(200, json={"profiles": [{"username": "grower"}]})
        handle = url.rstrip("/").split("/@")[-1]
        if handle == "grower":
            return httpx.Response(200, text=_profile_html(["G1", "G2"], followers=50))
        return httpx.Response(200, text=_profile_html(["S1"], followers=1))

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)

    result = asyncio.run(
        recommend_profiles(
            ["seed"],
            jev_url="http://jev.test/v1/evaluate",
            session_cookie="session=secret",
            expand_kinds=("following",),
        )
    )
    handles = [r.handle for r in result]
    assert "grower" in handles  # graph expansion reached the ranked set
    assert "seed" in handles  # seeds stay candidates
    assert result[0].handle == "grower"
    assert result[0].engine.startswith("jev:")


# ───────────────────────── taste-aware ranking ────────────────────────


def test_taste_titles_from_archive_mines_dedupes_and_strips_prefix(
    tmp_path: Path,
) -> None:
    """<dir>/<handle>/<handle>_<title>.m4a is the sync layout: parent dir is
    the authoritative prefix. m4a/mp4 pairs collapse to one title."""
    drinko = tmp_path / "downloads" / "drinko"
    drinko.mkdir(parents=True)
    (drinko / "drinko_Neon Skyline.m4a").touch()
    (drinko / "drinko_Neon Skyline.mp4").touch()  # pair → one title
    (drinko / "drinko_Agora on the Veranda.mp4").touch()
    (drinko / "legacy_flat_title.m4a").touch()  # tolerate legacy flat layout
    (drinko / "cover.jpg").touch()  # non-media ignored
    (tmp_path / "downloads" / "stray.txt").touch()  # loose files ignored
    (tmp_path / "downloads" / "empty_profile").mkdir()  # empty dir contributes nothing

    titles = _taste_titles_from_archive(tmp_path / "downloads")
    # Sorted (deterministic) walk; m4a/mp4 pairs dedupe to the first seen.
    assert titles == ["Agora on the Veranda", "Neon Skyline", "legacy flat title"]


def test_taste_titles_missing_or_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert _taste_titles_from_archive(tmp_path / "does-not-exist") == []
    (tmp_path / "nothing").mkdir()
    assert _taste_titles_from_archive(tmp_path / "nothing") == []


def test_recommend_profiles_jev_state_carries_taste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The taste titles must reach the JEV state payload, and the engine
    label must say so."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            return httpx.Response(200, text=_profile_html(["T1"], followers=3))
        body = json.loads(request.content)
        state = body["state"]
        assert '"Neon Skyline"' in state  # taste made it into the payload
        assert "similarity" in state.lower()
        return httpx.Response(
            200,
            json={"answers": {"pick": {"label": "alpha", "confidence": 0.7}}},
        )

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)

    result = asyncio.run(
        recommend_profiles(
            ["alpha", "beta"],
            jev_url="http://jev.test/v1/evaluate",
            taste_titles=["Neon Skyline", "Agora on the Veranda"],
        )
    )
    assert result[0].engine.endswith("+taste")


def test_taste_from_cli_wires_archive_and_warns_on_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI-level: --taste-from loads titles and reports the count; an empty
    archive warns without failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            return httpx.Response(200, text=_profile_html(["T1"], followers=3))
        return httpx.Response(
            200,
            json={"answers": {"pick": {"label": "alpha", "confidence": 0.7}}},
        )

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)

    archive = tmp_path / "archive"
    (archive / "drinko").mkdir(parents=True)
    (archive / "drinko" / "drinko_Neon Skyline.m4a").touch()
    runner = CliRunner()
    # Two candidates: a one-option question is refused by the decision client
    # (several providers 422 it) and honestly falls back to the heuristic.
    result = runner.invoke(
        recommend_cli,
        [
            "alpha",
            "beta",
            "--taste-from",
            str(archive),
            "--jev-url",
            "http://jev.test/v1/evaluate",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 archived titles" in result.output
    assert "+taste" in result.output

    empty = tmp_path / "empty-archive"
    empty.mkdir()
    warned = runner.invoke(
        recommend_cli,
        ["alpha", "--taste-from", str(empty), "--jev-url", "http://jev.test/v1/evaluate"],
    )
    assert warned.exit_code == 0, warned.output
    assert "no archived titles found" in warned.output
    assert "+taste" not in warned.output  # engine honest about missing signal


def test_recommend_profiles_ranks_via_laya_choice_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Laya (local /v1/decisions service) reports the winner as 'choice' with
    per-option probabilities — not the JEV 'label' field."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            return httpx.Response(200, text=_profile_html(["T1", "T2"], followers=9))
        body = json.loads(request.content)
        assert set(body["questions"]["pick"]["criteria"]) == {"alpha", "beta"}
        return httpx.Response(
            200,
            json={
                "model": "laya-rl-agent",
                "answers": {
                    "pick": {
                        "type": "choice",
                        "choice": "alpha",
                        "probabilities": {"alpha": 0.94, "beta": 0.06},
                        "confidence": 0.667,
                    }
                },
            },
        )

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    real_sync_init = httpx.Client.__init__

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)

    result = asyncio.run(
        recommend_profiles(["alpha", "beta"], jev_url="http://laya.test/v1/decisions")
    )
    assert result[0].handle == "alpha"  # Laya's 'choice' won
    assert result[0].engine.startswith("jev:")


def test_failed_jev_endpoint_credentials_never_reach_output_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A credential-bearing SUNO_JEV_URL stays out of labels, stdout/stderr and logs,
    including when the engine is unreachable and the heuristic fallback is used."""
    import logging

    secrets = ("synthetic-password", "synthetic-token", "jev.example")
    url = "https://user:synthetic-password@jev.example/v1/systemone?token=synthetic-token#frag"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "suno.com":
            return httpx.Response(200, text=_profile_html(["T1"], followers=5))
        return httpx.Response(503, json={"error": "down"})

    real_init = httpx.AsyncClient.__init__
    real_sync_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    def patched_sync_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)
    monkeypatch.setattr("suno_dl.recommend.ensure_dotenv_loaded", lambda: None)
    monkeypatch.setenv("SUNO_JEV_URL", url)

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(recommend_profiles(["alpha", "beta"], jev_url=url))
        cli = CliRunner().invoke(recommend_cli, ["alpha", "beta", "--jev-url", url])

    assert [r.engine for r in result] == ["heuristic", "heuristic"]
    captured = capsys.readouterr()
    everything = "\n".join(
        [
            captured.out,
            captured.err,
            cli.output,
            caplog.text,
            *(r.engine + r.rationale for r in result),
        ]
    )
    for secret in secrets:
        assert secret not in everything
