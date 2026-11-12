"""Cancel real Retry all subprocesses at deterministic staging boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from mutagen.mp4 import MP4

from suno_dl import cli, lossless_mp4, webui
from suno_dl.pacing import PlaybackPacer
from tests.conftest import _install_mock_transport
from tests.test_linux_downloads import PAYLOAD
from tests.test_producer_lookup import ID, OTHER, page
from tests.test_schedule_ui import backend

TITLES = {ID: "First song", OTHER: "Second song"}


def fixture_cli(phase: str) -> None:
    """Use the production CLI and publisher with disposable media and gates."""
    archive = Path.cwd()
    paused = archive / "paused"
    release = archive / "release"
    audio = PAYLOAD.read_bytes()

    class SlowMedia(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield audio * 64  # Fill the file buffer before exposing the gate.
            paused.touch()
            await asyncio.sleep(30)

    def provider(request: httpx.Request) -> httpx.Response:
        clip = request.url.path.rsplit("/", 1)[-1].removesuffix(".m4a")
        assert clip in TITLES
        if request.url.host == "suno.com":
            return httpx.Response(200, text=page(clip, title=TITLES[clip]))
        assert request.url.host == "cdn1.suno.ai"
        with (archive / "media-requests").open("a") as log:
            print(clip, file=log)
        if phase == "download" and clip == OTHER and not release.exists():
            return httpx.Response(200, stream=SlowMedia())
        return httpx.Response(200, content=audio)

    prepare_update = lossless_mp4._prepare_update

    def gated_tag(stream, values, *, cover=None):
        update = prepare_update(stream, values, cover=cover)
        if phase == "tagging" and values.get(b"\xa9nam") == TITLES[OTHER] and not release.exists():
            paused.touch()
            deadline = time.monotonic() + 20
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Tagging gate was not released")
                time.sleep(0.02)
        return update

    async def no_wait(self, duration):
        return 0.0

    with pytest.MonkeyPatch.context() as patch:
        _install_mock_transport(patch, provider)
        patch.setattr(PlaybackPacer, "wait", no_wait)
        patch.setattr(lossless_mp4, "_prepare_update", gated_tag)
        cli.main()


def poll(fn, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.025)
    pytest.fail("Timed out waiting for the retry")


def published(root):
    return {
        p.relative_to(root): (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sync uses SIGTERM")
@pytest.mark.parametrize("phase", ["download", "tagging"])
def test_cancel_retry_all_preserves_completed_and_resumes_unfinished(tmp_path, monkeypatch, phase):
    checkout = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(checkout / "src"), str(checkout))))
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "fixture")
    monkeypatch.setenv("SUNO_MIN_FREE_MB", "0")
    monkeypatch.setenv("SUNO_RATE_LIMIT_SEC", "0")
    command = webui._sync_command

    def fixture_command(*args, **kwargs):
        cmd = command(*args, **kwargs)
        assert cmd[2:4] == ["-m", "suno_dl.cli"]
        return [
            sys.executable,
            "-u",
            "-c",
            f"from tests.test_retry_cancellation import fixture_cli; fixture_cli({phase!r})",
            *cmd[4:],
        ]

    monkeypatch.setattr(webui, "_sync_command", fixture_command)
    release = tmp_path / "release"
    with backend(tmp_path, monkeypatch) as (port, _controller, manager):
        root = tmp_path / "music"
        for clip, title in TITLES.items():
            manager.failures.record({"id": clip, "title": title, "reason": "Fixture failure"})
        base = f"http://127.0.0.1:{port}"

        def start(count):
            response = httpx.post(
                base + "/api/sync",
                data={
                    "dry_run": "0",
                    "retry_failed": "1",
                    "retry_count": str(count),
                    "covers": "0",
                },
            )
            assert response.is_success, response.text
            return response.json()["id"]

        def status():
            return httpx.get(base + "/api/runs").json()

        first = start(2)
        try:
            poll((tmp_path / "paused").exists)
            name = "incoming.part" if phase == "download" else "audio.part"
            assert any(p.stat().st_size for p in (root / ".suno-staging").rglob(name))
            before = published(root)
            assert sorted(path.suffix for path in before) == [".json", ".m4a"]
            response = httpx.post(base + f"/api/runs/{first}/cancel")
            assert response.is_success, response.text
        finally:
            release.touch()
            if manager.busy:
                manager.cancel(first)
            poll(lambda: not manager.busy)

        data = status()
        assert data["runs"][0]["state"] == "cancelled"
        assert data["runs"][0]["returncode"] == 143
        assert data["last_retry_result"] == {
            "finished": data["runs"][0]["finished"],
            "recovered": 1,
            "failed": 0,
            "skipped": 0,
            "unfinished": 1,
            "stopped": True,
        }
        assert [row["id"] for row in data["failed_tracks"]] == [OTHER]
        assert published(root) == before
        assert not [p for p in (root / ".suno-staging").rglob("*") if p.is_file()]
        assert not list((root / ".library" / "verified-sync-intents").glob("*.json"))

        second = start(1)
        try:
            poll(lambda: not manager.busy)
        finally:
            if manager.busy:
                manager.cancel(second)
        data = status()
        assert data["runs"][0]["state"] == "done", data["runs"][0]["output"]
        assert data["runs"][0]["retry_ids"] == [OTHER]
        assert data["last_retry_result"]["recovered"] == 1
        assert data["last_retry_result"]["unfinished"] == 0
        assert data["failed_tracks"] == []
        after = published(root)
        assert len(after) == 4
        assert {path: after[path] for path in before} == before
        assert {
            json.loads(payload)["id"]
            for path, (_, _, payload) in after.items()
            if path.suffix == ".json"
        } == {ID, OTHER}
        assert {MP4(root / path)["\xa9nam"][0] for path in after if path.suffix == ".m4a"} == set(
            TITLES.values()
        )
        assert (tmp_path / "media-requests").read_text().splitlines() == [ID, OTHER, OTHER]
        assert not [p for p in (root / ".suno-staging").rglob("*") if p.is_file()]
