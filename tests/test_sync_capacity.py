"""Capacity estimates are informational, local-only and truthful about unknowns."""

import json
from types import SimpleNamespace as NS

from suno_dl.sync_capacity import (
    PREFIX,
    capacity_snapshot,
    destination_space,
    latest_capacity,
    queue_estimate,
)
from suno_dl.webui import Run, _compute_status


def test_estimate_known_unknown_zero_and_disabled_covers():
    known = queue_estimate([NS(duration_sec=240)], covers=False)
    assert known["count"] == 1
    assert known["estimated_bytes_min"] == 3840000
    assert known["estimated_bytes_max"] == 7680000
    mixed = queue_estimate([NS(duration_sec=240), NS(), NS(duration_sec=0)])
    assert mixed["duration_unknown"] == 2
    assert mixed["estimated_seconds"] == 720
    assert mixed["estimated_bytes_max"] > 3 * known["estimated_bytes_max"]
    empty = queue_estimate([])
    assert empty["count"] == empty["estimated_bytes_max"] == 0


def test_destination_uses_actual_filesystem_without_creating_output(tmp_path, monkeypatch):
    import suno_dl.sync_capacity as capacity

    calls = []
    monkeypatch.setattr(
        capacity.shutil, "disk_usage", lambda path: calls.append(path) or NS(free=123, total=456)
    )
    target = tmp_path / "not-created" / "music"
    space = destination_space(target)
    assert calls == [tmp_path]
    assert space["destination"] == str(target)
    assert space["free_bytes"] == 123
    assert not target.exists()

    def unavailable(_path):
        raise OSError("Mount unavailable")

    monkeypatch.setattr(capacity.shutil, "disk_usage", unavailable)
    assert destination_space(target)["available"] is False


def test_latest_queue_drives_api_snapshot_and_rejects_bogus_output(tmp_path):
    capacity = capacity_snapshot(tmp_path, [NS(duration_sec=180)], covers=False)
    line = PREFIX + json.dumps(capacity)
    run = Run(id="offline", kind="sync", cmd=[])
    run.output = [line, PREFIX + "not-json", PREFIX + '{"queue":null}']
    assert run.snapshot()["capacity"]["queue"]["count"] == 1
    assert latest_capacity([PREFIX + '{"queue":{"count":-1}}']) is None
    assert latest_capacity([PREFIX + "[]"]) is None
    run.output.append(PREFIX + json.dumps(capacity_snapshot(tmp_path, [])))
    assert run.snapshot()["capacity"]["queue"]["count"] == 0


def test_status_measures_configured_destination_not_archive(tmp_path, monkeypatch):
    import suno_dl.webui as webui

    target = tmp_path / "other-volume" / "music"
    monkeypatch.setattr(webui, "destination_space", lambda p: {"destination": str(p)})
    assert _compute_status(tmp_path, {"SUNO_OUTPUT_DIR": str(target)})["storage"] == {
        "destination": str(target)
    }
