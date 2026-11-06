"""Status publication never changes media or follows a replaced status link."""

import json

import pytest

from suno_dl.sync_state import publish_status


def test_status_is_readable_and_rejects_links_and_invalid_states(tmp_path):
    (tmp_path / ".library").mkdir()
    audio = tmp_path / "existing.m4a"
    audio.write_bytes(b"unchanged media")
    target = publish_status(tmp_path, {"phase": "waiting", "missing": 85, "downloaded": 1})
    assert json.loads(target.read_text())["downloaded"] == 1
    with pytest.raises(ValueError):
        publish_status(tmp_path, {"phase": "invented"})
    before = target.read_bytes()
    with pytest.raises(ValueError):
        publish_status(tmp_path, {"phase": "checking", "message": "x" * 70000})
    assert target.read_bytes() == before
    target.unlink()
    target.symlink_to(audio)
    with pytest.raises(ValueError):
        publish_status(tmp_path, {"phase": "complete"})
    assert audio.read_bytes() == b"unchanged media"
