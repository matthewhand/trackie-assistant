"""ffmpeg discovery: PATH, the ``SUNO_FFMPEG`` override, and refusal to guess."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from suno_dl.ffmpeg import find_ffmpeg


def test_returns_none_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("suno_dl.ffmpeg.shutil.which", lambda _name: None)
    monkeypatch.delenv("SUNO_FFMPEG", raising=False)
    assert find_ffmpeg() is None


def test_finds_ffmpeg_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("suno_dl.ffmpeg.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.delenv("SUNO_FFMPEG", raising=False)
    assert find_ffmpeg() == "/usr/bin/ffmpeg"


def test_env_override_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "my-ffmpeg"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr("suno_dl.ffmpeg.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setenv("SUNO_FFMPEG", str(binary))
    assert find_ffmpeg() == str(binary)


@pytest.mark.skipif(
    os.name == "nt", reason="Windows has no execute bit: every existing file passes os.access(X_OK)"
)
@pytest.mark.parametrize("mode", [0o644, 0o600])
def test_non_executable_override_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """A wrong override must not silently tag with some other binary."""
    binary = tmp_path / "not-ffmpeg"
    binary.write_text("nope")
    binary.chmod(mode)
    monkeypatch.setattr("suno_dl.ffmpeg.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setenv("SUNO_FFMPEG", str(binary))
    assert find_ffmpeg() == "/usr/bin/ffmpeg"


def test_missing_override_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("suno_dl.ffmpeg.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setenv("SUNO_FFMPEG", str(tmp_path / "gone"))
    assert find_ffmpeg() == "/usr/bin/ffmpeg"


def test_real_environment_resolves_to_a_runnable_binary() -> None:
    """Smoke: the real lookup never returns a path that cannot be executed."""
    found = find_ffmpeg()
    if found is not None:
        assert Path(found).is_file() and os.access(found, os.X_OK)
