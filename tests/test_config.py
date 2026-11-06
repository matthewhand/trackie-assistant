"""Config and URL-kind tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from suno_dl.config import load_config


def test_default_url_from_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.delenv("SUNO_DEFAULT_URL", raising=False)
    cfg = load_config()
    assert cfg.build_default_url() == "https://suno.com/@drinko"


def test_explicit_default_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "")
    monkeypatch.setenv("SUNO_DEFAULT_URL", "https://suno.com/song/abc")
    cfg = load_config()
    assert cfg.build_default_url() == "https://suno.com/song/abc"


def test_requires_a_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "")
    monkeypatch.delenv("SUNO_DEFAULT_URL", raising=False)
    with pytest.raises(ValueError, match="SUNO_DEFAULT"):
        load_config()


def test_rejects_zero_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_CONCURRENCY", "0")
    with pytest.raises(ValueError):
        load_config()


def test_sanitize() -> None:
    from suno_dl.naming import sanitize_component

    assert sanitize_component('A/B:C*D?"E<>') == "A_B_C_D_E"
    assert sanitize_component("  spaced   out  ") == "spaced out"
    assert sanitize_component("") == "untitled"
    assert sanitize_component(".") == "untitled"
    assert sanitize_component("..") == "untitled"


def test_watch_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_WATCH", " @a , @b ,, https://suno.com/playlist/x ,")
    cfg = load_config()
    assert cfg.watch == ["@a", "@b", "https://suno.com/playlist/x"]


def test_watch_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.delenv("SUNO_WATCH", raising=False)
    assert load_config().watch == []


def test_watch_alone_satisfies_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "")
    monkeypatch.delenv("SUNO_DEFAULT_URL", raising=False)
    monkeypatch.setenv("SUNO_WATCH", "@a,@b")
    cfg = load_config()
    assert cfg.watch == ["@a", "@b"]


def test_env_loaded_from_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .env in the user's CWD must be picked up (not the package's dir)."""
    (tmp_path / ".env").write_text("SUNO_WATCH=@x\nSUNO_DEFAULT_HANDLE=drinko\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_WATCH", raising=False)
    monkeypatch.delenv("SUNO_DEFAULT_HANDLE", raising=False)
    cfg = load_config()
    assert cfg.watch == ["@x"]


def test_min_free_mb_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.delenv("SUNO_MIN_FREE_MB", raising=False)
    cfg = load_config()
    assert cfg.min_free_mb == 2048


def test_min_free_mb_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_MIN_FREE_MB", "512")
    cfg = load_config()
    assert cfg.min_free_mb == 512


def test_min_free_mb_cli_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_MIN_FREE_MB", "1024")
    cfg = load_config(min_free_mb=256)
    assert cfg.min_free_mb == 256


def test_min_free_mb_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_MIN_FREE_MB", "-1")
    with pytest.raises(ValueError):
        load_config()


def test_media_defaults_videos_off_covers_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.delenv("SUNO_DOWNLOAD_VIDEOS", raising=False)
    monkeypatch.delenv("SUNO_COVERS", raising=False)
    cfg = load_config()
    assert cfg.download_videos is False
    assert cfg.covers is True


def test_media_env_bools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_DOWNLOAD_VIDEOS", "true")
    monkeypatch.setenv("SUNO_COVERS", "0")
    cfg = load_config()
    assert cfg.download_videos is True
    assert cfg.covers is False


def test_caps_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """#93: no cap and no rotation unless explicitly asked for."""
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.delenv("SUNO_MAX_TRACKS", raising=False)
    monkeypatch.delenv("SUNO_MAX_DISK_MB", raising=False)
    monkeypatch.delenv("SUNO_ROTATE", raising=False)
    cfg = load_config()
    assert cfg.max_tracks == 0
    assert cfg.max_disk_mb == 0
    assert cfg.rotate is False


def test_caps_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_MAX_TRACKS", "20")
    monkeypatch.setenv("SUNO_MAX_DISK_MB", "1024")
    monkeypatch.setenv("SUNO_ROTATE", "1")
    cfg = load_config()
    assert cfg.max_tracks == 20
    assert cfg.max_disk_mb == 1024
    assert cfg.rotate is True


def test_caps_overridden_by_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI flags win over the environment; unset flags leave it alone."""
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_MAX_TRACKS", "20")
    monkeypatch.setenv("SUNO_MAX_DISK_MB", "0")
    cfg = load_config(max_tracks=5, max_disk_mb=None, rotate=None)
    assert cfg.max_tracks == 5
    assert cfg.max_disk_mb == 0
    assert cfg.rotate is False
