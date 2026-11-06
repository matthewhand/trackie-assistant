"""The release preflight must fail closed while capabilities are unavailable."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "release_readiness.py"
    spec = importlib.util.spec_from_file_location("release_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_known_capability_blocks_stop_release(monkeypatch, capsys):
    import suno_dl.archive_lock as archive_lock
    import suno_dl.linux_downloads as linux_downloads
    import suno_dl.producer_lookup as producer_lookup
    import suno_dl.tagging as tagging
    from suno_dl.download_intent import WindowsArchiveWriteUnavailable

    def unavailable(error):
        def fail():
            raise error("unavailable")

        return fail

    monkeypatch.setattr(
        producer_lookup,
        "_require_trusted_framing",
        unavailable(producer_lookup.ProducerLookupError),
    )
    monkeypatch.setattr(
        linux_downloads, "require_audio_sync", unavailable(tagging.TaggingUnavailable)
    )
    monkeypatch.setattr(
        archive_lock,
        "raise_if_unsupported_archive_write",
        unavailable(WindowsArchiveWriteUnavailable),
    )
    readiness = _load()
    assert readiness.main() == 1
    err = capsys.readouterr().err
    assert "producer verification" in err
    assert "safe Linux audio sync" in err
    assert "archive writes on this platform" in err


def test_unknown_check_failure_is_not_ignored():
    readiness = _load()

    def bad():
        raise ValueError("unexpected failure")

    with pytest.raises(ValueError, match="unexpected failure"):
        readiness.active_blockers([("broken", bad, RuntimeError)])


def test_open_checks_are_not_reported_as_blockers():
    readiness = _load()
    assert readiness.active_blockers([("ready", lambda: None, RuntimeError)]) == []


def test_real_windows_platform_gate_blocks_release(monkeypatch, capsys):
    import suno_dl.archive_lock as archive_lock
    import suno_dl.linux_downloads as linux_downloads
    import suno_dl.producer_lookup as producer_lookup

    # Exercise the actual Windows gate, without creating any archive files.
    monkeypatch.setattr(producer_lookup, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(linux_downloads, "require_audio_sync", lambda: None)
    readiness = _load()
    with monkeypatch.context() as scoped:
        scoped.setattr(archive_lock.sys, "platform", "win32")
        assert readiness.main() == 1
    assert "archive writes on this platform" in capsys.readouterr().err
