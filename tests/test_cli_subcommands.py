"""Command-group routing: a bare invocation is `download`; `network-styles` is a subcommand."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from suno_dl.cli import EXIT_NO_TRACKS, EXIT_OK, main

OK_STATS = {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("SUNO_DOWNLOAD_VIDEOS", raising=False)
    monkeypatch.delenv("SUNO_COVERS", raising=False)
    monkeypatch.chdir(tmp_path)


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_run(*args: Any, **kwargs: Any) -> dict[str, int]:
        calls.append({"args": args, "kwargs": kwargs})
        return dict(OK_STATS)

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    return calls


def test_bare_flags_route_to_download(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(monkeypatch)
    result = CliRunner().invoke(
        main,
        ["--url", "https://suno.com/@drinko", "--model-version", "v6", "--min-free-mb", "5"],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert len(calls) == 1
    assert calls[0]["kwargs"]["model_version"] == "v6"
    assert calls[0]["kwargs"]["min_free_mb"] == 5


def test_explicit_download_matches_bare_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(monkeypatch)
    argv = ["--url", "https://suno.com/@drinko", "--model-version", "v6", "--min-free-mb", "5"]
    bare = CliRunner().invoke(main, argv)
    explicit = CliRunner().invoke(main, ["download", *argv])
    assert bare.exit_code == explicit.exit_code == EXIT_OK
    assert len(calls) == 2
    assert calls[0]["args"][0] == calls[1]["args"][0] == "https://suno.com/@drinko"
    assert calls[0]["kwargs"] == calls[1]["kwargs"]


def test_no_arguments_is_download(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"total": 0, "downloaded": 0, "skipped_exists": 0, "failed": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(main, [])
    assert result.exit_code == EXIT_NO_TRACKS


def test_download_help_lists_model_version_and_min_free_mb() -> None:
    for argv in (["--help"], ["download", "--help"], ["-h"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0
        assert "--model-version" in result.output
        assert "--min-free-mb" in result.output


def test_network_styles_subcommand_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_network_styles(**kwargs: Any) -> None:
        seen.update(kwargs)

    async def fail_run(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise AssertionError("download must not run for network-styles")

    monkeypatch.setattr("suno_dl.cli._run_network_styles", fake_network_styles)
    monkeypatch.setattr("suno_dl.cli._run", fail_run)
    monkeypatch.delenv("SUNO_SESSION_COOKIE", raising=False)
    result = CliRunner().invoke(
        main, ["network-styles", "--handle", "drinko", "--network-cap", "3"]
    )
    assert result.exit_code == 0, result.output
    assert seen["handle"] == "drinko"
    assert seen["network_cap"] == 3
    assert seen["session_cookie"] is None


def test_network_styles_requires_handle() -> None:
    result = CliRunner().invoke(main, ["network-styles"])
    assert result.exit_code != 0
    assert "--handle" in result.output


def test_network_styles_help() -> None:
    result = CliRunner().invoke(main, ["network-styles", "--help"])
    assert result.exit_code == 0
    assert "--network-cap" in result.output
