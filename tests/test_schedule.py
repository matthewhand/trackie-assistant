"""Scheduling controls share the actual run admission gate and never cancel media."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest

from suno_dl import schedule
from suno_dl.webui import _sync_command


@dataclass
class FakeRun:
    id: str
    started: float
    finished: float | None = None
    returncode: int | None = None

    def snapshot(self):
        return {
            "id": self.id,
            "kind": "sync",
            "state": "running" if self.returncode is None else "done",
            "started": self.started,
            "finished": self.finished,
            "returncode": self.returncode,
            "output": [],
            "error": None,
        }


class Manager:
    def __init__(self, clock):
        self.clock = clock
        self.runs = []
        self.commands = []

    @property
    def busy(self):
        return any(r.returncode is None for r in self.runs)

    def list(self):
        return [r.snapshot() for r in reversed(self.runs)]

    def start(self, kind, command):
        if self.busy:
            return None, "busy"
        self.commands.append(command)
        run = FakeRun(str(len(self.runs)), self.clock())
        self.runs.append(run)
        return run, None


@pytest.fixture
def backend(tmp_path):
    now = [1000.0]
    manager = Manager(lambda: now[0])
    controller = schedule.Scheduler(
        tmp_path,
        manager,
        lambda src: _sync_command(False, sources=src),
        ["@example"],
        clock=lambda: now[0],
    )
    yield controller, manager, now
    controller.close()


def settings(enabled=True, interval=360):
    return {"enabled": enabled, "interval_minutes": interval, "sources": ["@example"]}


def test_enable_pause_and_interval_changes_do_not_cancel_current_download(backend):
    controller, manager, now = backend
    controller.save(settings())
    controller.tick()
    assert len(manager.commands) == 1
    command = manager.commands[0]
    assert "--playlists" in command and "--no-rotate" in command
    assert "--no-videos" in command and "--covers" in command
    assert "--update-existing" not in command and "--force-redownload" not in command
    now[0] += 100000
    controller.tick()  # due time passed, but the actual manager is still busy
    assert len(manager.commands) == 1
    controller.save(settings(False, 60))
    controller.tick()
    assert manager.runs[0].returncode is None
    assert controller.snapshot()["next_run"] is None
    manager.runs[0].returncode = 0
    manager.runs[0].finished = now[0]
    controller.tick()
    assert len(manager.commands) == 1
    controller.save(settings(True, 60))
    controller.tick()
    assert len(manager.commands) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"interval_minutes": 14},
        {"interval_minutes": 60.5},
        {"sources": ["https://suno.com/@example?token=unwanted"]},
    ],
)
def test_invalid_settings_never_publish_or_start_a_run(backend, change):
    controller, manager, _ = backend
    controller.save(settings(False))
    before = controller.path.read_bytes()
    value = dict(settings(), **change)
    with pytest.raises(ValueError):
        controller.save(value)
    assert controller.path.read_bytes() == before
    assert manager.commands == []


def test_restart_retains_deadline_and_refuses_second_scheduler(tmp_path):
    now = [1000.0]
    manager = Manager(lambda: now[0])
    first = schedule.Scheduler(
        tmp_path, manager, lambda src: list(src), ["@example"], clock=lambda: now[0]
    )
    try:
        first.save(settings())
        first.tick()
        due = first.snapshot()["next_run"]
        with pytest.raises(BlockingIOError):
            schedule.Scheduler(tmp_path, manager, lambda src: list(src), ["@example"])
    finally:
        first.close()
    restarted = schedule.Scheduler(
        tmp_path,
        Manager(lambda: now[0]),
        lambda src: list(src),
        ["@example"],
        clock=lambda: now[0],
    )
    try:
        assert restarted.snapshot()["next_run"] == due
        assert restarted.snapshot()["last_run"]["state"] == "failed"
        restarted.tick()
        assert restarted.manager.commands == []
    finally:
        restarted.close()


def test_failed_pause_publication_pauses_this_backend_even_when_disk_is_uncertain(
    backend, monkeypatch
):
    controller, manager, _ = backend
    controller.save(settings())
    write = schedule._write

    def uncertain(path, data):
        write(path, data)
        raise OSError("injected directory durability failure")

    monkeypatch.setattr(schedule, "_write", uncertain)
    with pytest.raises(OSError):
        controller.save(settings(False))
    assert controller.snapshot()["enabled"] is False
    controller.tick()
    assert manager.commands == []


def test_completed_manual_sync_delays_scheduled_check_and_reconfiguration_survives(backend):
    controller, manager, now = backend
    controller.save(settings())
    manager.start("sync", ["manual"])
    now[0] += 50
    manager.runs[0].returncode = 0
    manager.runs[0].finished = now[0]
    controller.tick()
    assert controller.snapshot()["next_run"] == now[0] + 360 * 60
    controller.save(settings(True, 90))
    assert controller.snapshot()["next_run"] == now[0] + 90 * 60
    persisted = json.loads(controller.path.read_text())
    assert persisted["last_run"] == copy.deepcopy(manager.runs[0].snapshot())


def test_completed_run_does_not_rewrite_state_on_every_idle_poll(backend):
    controller, manager, now = backend
    controller.save(settings())
    controller.tick()
    manager.runs[0].returncode = 0
    manager.runs[0].finished = now[0]
    controller.tick()
    before = controller.path.stat().st_mtime_ns
    for _ in range(10):
        controller.tick()
    assert controller.path.stat().st_mtime_ns == before


def test_corrupt_state_is_paused_and_can_be_reconfigured(tmp_path):
    folder = tmp_path / ".scheduler"
    folder.mkdir(mode=0o700)
    (folder / "state.json").write_text("{broken")
    manager = Manager(lambda: 1000)
    controller = schedule.Scheduler(tmp_path, manager, lambda src: list(src), ["@example"])
    try:
        controller.tick()
        assert not controller.snapshot()["enabled"] and manager.commands == []
        assert "could not be read" in controller.snapshot()["notice"]
        controller.save(settings())
        assert "notice" not in controller.snapshot()
    finally:
        controller.close()
