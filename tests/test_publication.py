"""Prepared publication recovery and strict state validation."""

import json
import os

import pytest

from suno_dl.publication import publish_prepared
from tests.test_verified_linux_sync import ID, intent


def v2_group(root):
    marker, output = intent(root)
    data = json.loads(marker.read_text())
    data["schema"] = 2
    marker.write_text(json.dumps(data))
    return marker, output


@pytest.mark.parametrize("phase", ["first_source_removed", "all_sources_removed", "stage_removed"])
def test_cleanup_interrupt_keeps_intent_and_recovers_exact_group(tmp_path, monkeypatch, phase):
    marker, output = v2_group(tmp_path)
    real_unlink, real_rmdir = os.unlink, os.rmdir
    count = 0

    def unlink(path, *args, **kwargs):
        nonlocal count
        result = real_unlink(path, *args, **kwargs)
        if str(path).endswith(("audio.m4a", "sidecar.json")) and ".suno-staging" in str(path):
            count += 1
            if (phase == "first_source_removed" and count == 1) or (
                phase == "all_sources_removed" and count == 2
            ):
                raise OSError("injected cleanup stop")
        return result

    def rmdir(path, *args, **kwargs):
        result = real_rmdir(path, *args, **kwargs)
        if phase == "stage_removed" and ".suno-staging" in str(path):
            raise OSError("injected stop after private directory removal")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "unlink", unlink)
        scoped.setattr(os, "rmdir", rmdir)
        with pytest.raises(OSError):
            publish_prepared(tmp_path, marker)
    assert marker.exists()
    assert publish_prepared(tmp_path, marker) == ID
    assert not marker.exists()
    assert len(list(output.glob("*.m4a"))) == len(list(output.glob("*.json"))) == 1
    assert all(p.stat().st_nlink == 1 for p in output.iterdir())


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_id",
        "nonfinite",
        "wrong_uuid",
        "foreign_hardlink",
        "extra_file",
        "duplicate_destination",
    ],
)
def test_malformed_intent_cannot_publish_any_member(tmp_path, change):
    marker, output = v2_group(tmp_path)
    data = json.loads(marker.read_text())
    if change == "wrong_uuid":
        data["id"] = "22222222-2222-4222-8222-222222222222"
    elif change == "foreign_hardlink":
        os.link(tmp_path / data["members"][0]["stage"], tmp_path / "unowned-alias")
    elif change == "extra_file":
        data["members"].append(dict(data["members"][0], destination="drinko/extra.exe"))
    elif change == "duplicate_destination":
        data["members"][1]["destination"] = data["members"][0]["destination"]
    raw = json.dumps(data)
    if change == "duplicate_id":
        raw = raw.replace('"id":', '"id":"bogus","id":', 1)
    elif change == "nonfinite":
        raw = raw[:-1] + ',"bad":NaN}'
    marker.write_text(raw)
    with pytest.raises(ValueError):
        publish_prepared(tmp_path, marker)
    assert not list(output.iterdir()) and marker.exists()
