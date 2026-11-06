"""No pending, wrong, failed, partial or torn run may authorize cutover."""

import json

import pytest

from suno_dl.sync_acceptance import SyncNotReady, verify_terminal_success

ID = "0120588c-08fd-477c-9386-5108bd97179a"
CLIP = "0b04ccf5-b80a-4ce4-80ae-ef9aa95cb0be"
CONTAINER = "a" * 64


def inputs(tmp_path):
    rows = [
        {"event": "started", "id": ID, "total": 2, "existing": 1, "missing": 1},
        {"event": "downloaded", "id": CLIP},
        {"event": "finished", "downloaded": 1, "failed": 0, "total": 2},
    ]
    path = tmp_path / "audit.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))
    state = {"Id": CONTAINER, "State": {"Status": "exited", "Running": False, "ExitCode": 0}}
    return path, rows, state


def save(path, rows):
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))


def test_exact_success_is_read_only(tmp_path):
    path, rows, state = inputs(tmp_path)
    before = path.read_bytes()
    result = verify_terminal_success(path, ID, CONTAINER, state)
    assert result["run_id"] == ID and len(result["downloads"]) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "problem",
    [
        "running",
        "wrong_container",
        "exit_failure",
        "pending",
        "unavailable",
        "partial",
        "duplicate",
        "new_run",
        "torn",
        "duplicate_field",
    ],
)
def test_unsafe_terminal_state_is_rejected_without_changes(tmp_path, problem):
    path, rows, state = inputs(tmp_path)
    if problem == "running":
        state["State"].update(Status="running", Running=True)
    elif problem == "wrong_container":
        state["Id"] = "b" * 64
    elif problem == "exit_failure":
        state["State"]["ExitCode"] = 1
    elif problem == "pending":
        rows.pop()
    elif problem == "unavailable":
        rows.insert(2, {"event": "unavailable", "id": CLIP})
    elif problem == "partial":
        rows[-1]["downloaded"] = 0
    elif problem == "duplicate":
        rows.insert(2, dict(rows[1]))
    elif problem == "new_run":
        rows.append({"event": "started", "id": "11111111-1111-4111-8111-111111111111"})
    save(path, rows)
    if problem == "torn":
        path.write_bytes(path.read_bytes()[:-1])
    if problem == "duplicate_field":
        path.write_text(path.read_text().replace('"failed": 0', '"failed":1,"failed":0'))
    before = path.read_bytes()
    with pytest.raises(SyncNotReady):
        verify_terminal_success(path, ID, CONTAINER, state)
    assert path.read_bytes() == before


def test_mixed_case_duplicate_clip_uuid_is_rejected(tmp_path):
    path, rows, state = inputs(tmp_path)
    rows[0].update(total=3, missing=2)
    rows.insert(2, {"event": "downloaded", "id": CLIP.upper()})
    rows[-1].update(downloaded=2, total=3)
    save(path, rows)
    with pytest.raises(SyncNotReady, match="accounting"):
        verify_terminal_success(path, ID, CONTAINER, state)


@pytest.mark.parametrize("raw", ["[]\n", "null\n", "1\n", '"object"\n'])
def test_non_object_audit_records_fail_closed(tmp_path, raw):
    path, _, state = inputs(tmp_path)
    path.write_text(raw)
    with pytest.raises(SyncNotReady, match="objects"):
        verify_terminal_success(path, ID, CONTAINER, state)


def test_audit_path_swap_is_rejected(tmp_path, monkeypatch):
    import suno_dl.sync_acceptance as acceptance

    path, rows, state = inputs(tmp_path)
    replacement = tmp_path / "other"
    save(replacement, rows)
    original = acceptance.os.open

    def swapped(name, flags):
        replacement.replace(path)
        return original(name, flags)

    monkeypatch.setattr(acceptance.os, "open", swapped)
    with pytest.raises(SyncNotReady, match="changed"):
        verify_terminal_success(path, ID, CONTAINER, state)


def test_linked_audit_rejected_without_reading(tmp_path):
    import os

    path, _, state = inputs(tmp_path)
    os.link(path, tmp_path / "linked")
    with pytest.raises(SyncNotReady, match="unsafe"):
        verify_terminal_success(path, ID, CONTAINER, state)
