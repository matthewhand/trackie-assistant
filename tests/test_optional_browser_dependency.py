"""Lean-install dependency and browser-fallback contract tests (#125)."""

from __future__ import annotations

import asyncio
import builtins
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

from suno_dl.discovery import discover_profile_tracks_playwright

ROOT = Path(__file__).resolve().parents[1]


def _project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_base_install_has_no_browser_dependency() -> None:
    dependencies = _project()["dependencies"]
    assert isinstance(dependencies, list)
    assert not any(str(dep).lower().startswith("playwright") for dep in dependencies)


def test_optional_browser_extra_and_dev_tests_have_playwright() -> None:
    extras = _project()["optional-dependencies"]
    assert isinstance(extras, dict)
    for extra in ("browser", "dev"):
        assert any(str(dep).startswith("playwright>=") for dep in extras[extra])


def test_vendored_addon_wheel_metadata_keeps_browser_optional() -> None:
    """The vendored add-on wheel must not re-add Playwright to base requirements."""
    wheels = sorted((ROOT / "addon/suno_dl").glob("*.whl"))
    assert len(wheels) == 1, [wheel.name for wheel in wheels]
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    requirements = [str(req).lower() for req in metadata.get_all("Requires-Dist") or []]
    unconditional = [req for req in requirements if "extra ==" not in req]
    assert not any(req.startswith("playwright") for req in unconditional), requirements
    assert any(req.startswith("playwright") and 'extra == "browser"' in req for req in requirements)


def test_addon_image_installs_browser_fallback_explicitly() -> None:
    """Removing Playwright from the wheel must not break the add-on image."""
    dockerfile = (ROOT / "addon/suno_dl/Dockerfile").read_text()
    install = [
        line
        for line in dockerfile.splitlines()
        if line.startswith("RUN pip install") and "trackie_assistant-*.whl" in line
    ]
    assert install, dockerfile
    assert "playwright" in install[0]
    assert "playwright install --only-shell chromium" in dockerfile


def test_missing_browser_dependency_has_install_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Chromium invocation when the optional Python library is missing."""
    original_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright":
            raise ImportError("synthetic browser dependency absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"trackie-assistant\[browser\]"):
        asyncio.run(discover_profile_tracks_playwright("https://suno.com/@example", "example"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("feed_mode", ["empty", "error"])
@pytest.mark.parametrize("browser_args", [(), ("--browser",)])
def test_cli_strict_fallback_requires_optional_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    feed_mode: str,
    browser_args: tuple[str, ...],
) -> None:
    """A missing browser fails *clearly* by default, never silently omits tracks."""
    from click.testing import CliRunner

    from suno_dl.cli import main

    async def fake_feed(*args: object, **kwargs: object) -> list[object]:
        if feed_mode == "error":
            raise RuntimeError("synthetic HTTP feed outage")
        return []

    original_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright":
            raise ImportError("synthetic optional browser absence")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("suno_dl.discovery.discover_profile_tracks_feed", fake_feed)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "synthetic")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@synthetic", "--dry-run", *browser_args]
    )
    assert result.exit_code == 1, result.output
    assert "Browser fallback requires Playwright" in result.output
    assert "trackie-assistant[browser]" in result.output
    assert "Traceback" not in result.output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("feed_mode", ["empty", "error"])
def test_cli_no_browser_is_explicit_feed_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    feed_mode: str,
) -> None:
    """Explicitly disabled browser returns no tracks, with no install error."""
    from click.testing import CliRunner

    from suno_dl.cli import EXIT_NO_TRACKS, main

    async def fake_feed(*args: object, **kwargs: object) -> list[object]:
        if feed_mode == "error":
            raise RuntimeError("synthetic HTTP feed outage")
        return []

    original_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright":
            raise AssertionError("disabled browser fallback must never import Playwright")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("suno_dl.discovery.discover_profile_tracks_feed", fake_feed)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "synthetic")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@synthetic", "--no-browser", "--dry-run"]
    )
    assert result.exit_code == EXIT_NO_TRACKS, result.output
    assert "Found 0 public track(s)." in result.output
    assert "trackie-assistant[browser]" not in result.output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("feed_mode", ["empty", "error"])
@pytest.mark.parametrize("browser_args", [(), ("--browser",)])
def test_missing_chromium_executable_reports_install_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    feed_mode: str,
    browser_args: tuple[str, ...],
) -> None:
    """A browser Python package without Chromium must fail with CLI guidance."""
    from click.testing import CliRunner
    from playwright.sync_api import Error as PlaywrightError

    from suno_dl.cli import main

    async def fake_feed(*args: object, **kwargs: object) -> list[object]:
        if feed_mode == "error":
            raise RuntimeError("synthetic HTTP feed failure")
        return []

    def fake_browser(*args: object, **kwargs: object) -> None:
        raise PlaywrightError("BrowserType.launch: Executable doesn't exist at /synthetic/chromium")

    monkeypatch.setattr("suno_dl.discovery.discover_profile_tracks_feed", fake_feed)
    monkeypatch.setattr("suno_dl.discovery._discover_sync", fake_browser)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "synthetic")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@synthetic", "--dry-run", *browser_args]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Playwright Chromium is not installed" in result.output
    assert "python -m playwright install chromium" in result.output
    assert "--no-browser" in result.output
    assert "Traceback" not in result.output
    assert not list(tmp_path.iterdir()), "dry-run should not create downloaded files"


@pytest.mark.parametrize(
    "message",
    [
        "BrowserType.launch: net::ERR_CONNECTION_REFUSED",
        "Page.goto: Navigation timeout of 60000 ms exceeded",
        "BrowserType.launch: Target page, context or browser has been closed",
        "Executable doesn't exist at /synthetic/chromium",  # no launch context
    ],
)
def test_unrelated_playwright_error_is_not_misdiagnosed(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """Avoid hiding unrelated browser errors behind Chromium install advice."""
    from playwright.sync_api import Error as PlaywrightError

    def fake_browser(*args: object, **kwargs: object) -> None:
        raise PlaywrightError(message)

    monkeypatch.setattr("suno_dl.discovery._discover_sync", fake_browser)
    with pytest.raises(PlaywrightError) as caught:
        asyncio.run(discover_profile_tracks_playwright("https://suno.com/@synthetic", "synthetic"))
    assert str(caught.value) == message


def test_missing_chromium_does_not_affect_successful_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP feed success must never attempt Chromium, even with fallback enabled."""
    from suno_dl.discovery import discover_profile_tracks

    track = object()

    async def fake_feed(*args: object, **kwargs: object) -> list[object]:
        return [track]

    def forbidden_browser(*args: object, **kwargs: object) -> None:
        raise AssertionError("browser attempted when HTTP feed succeeded")

    monkeypatch.setattr("suno_dl.discovery.discover_profile_tracks_feed", fake_feed)
    monkeypatch.setattr("suno_dl.discovery._discover_sync", forbidden_browser)
    tracks = asyncio.run(discover_profile_tracks("https://suno.com/@synthetic", "synthetic"))
    assert tracks == [track]


@pytest.mark.parametrize("mode", ["direct", "sync"])
@pytest.mark.parametrize("missing", ["package", "chromium"])
def test_cli_browser_guidance_does_not_echo_untrusted_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, missing: str
) -> None:
    from click.testing import CliRunner

    from suno_dl.cli import main
    from suno_dl.discovery import BrowserDependencyMissing, ChromiumExecutableMissing

    async def fail(*args: object, **kwargs: object) -> None:
        cls = BrowserDependencyMissing if missing == "package" else ChromiumExecutableMissing
        raise cls("failure https://example.test/?token=SECRET_TOKEN")

    monkeypatch.setattr("suno_dl.cli._run", fail)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "synthetic")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))
    args = (
        ["--url", "https://suno.com/@synthetic"]
        if mode == "direct"
        else ["--sync", "--watch", "@synthetic"]
    )
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    if missing == "package":
        assert "Browser fallback requires Playwright" in result.output
        assert "trackie-assistant[browser]" in result.output
    else:
        assert "Playwright Chromium is not installed" in result.output
        assert "python -m playwright install chromium" in result.output
    assert "--no-browser" in result.output
    assert "SECRET_TOKEN" not in result.output


def test_missing_chromium_traceback_does_not_expose_playwright_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback

    from playwright.sync_api import Error as PlaywrightError

    from suno_dl.discovery import ChromiumExecutableMissing

    def fake_browser(*args: object, **kwargs: object) -> None:
        raise PlaywrightError(
            "BrowserType.launch: Executable doesn't exist at /synthetic/chromium "
            "https://example.test/?token=SECRET_TOKEN"
        )

    monkeypatch.setattr("suno_dl.discovery._discover_sync", fake_browser)
    with pytest.raises(ChromiumExecutableMissing) as caught:
        asyncio.run(discover_profile_tracks_playwright("https://suno.com/@synthetic", "synthetic"))
    formatted = "".join(traceback.format_exception(caught.value))
    assert "Playwright Chromium is not installed" in formatted
    assert "python -m playwright install chromium" in formatted
    assert "SECRET_TOKEN" not in formatted
