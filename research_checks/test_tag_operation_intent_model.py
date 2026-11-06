import os
import subprocess
import sys

import pytest

from tests import tag_operation_intent_model as model

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only intent model")


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".library").mkdir()
    return tmp_path


def test_durable_create_detect_and_inspected_resolution(root):
    assert model.inspect_pending(root, lock_held=True) is None
    intent_id = model.begin(
        root, "metadata", ["folder/song.m4a", "folder/song.json"], lock_held=True
    )
    found = model.inspect_pending(root, lock_held=True)
    assert found is not None
    assert found["id"] == intent_id
    assert found["paths"] == ["folder/song.m4a", "folder/song.json"]
    with pytest.raises(model.UnsafeOperationIntent):
        model.begin(root, "metadata", ["other"], lock_held=True)
    with pytest.raises(model.UnsafeOperationIntent, match="does not match"):
        model.resolve_after_inspection(root, "incorrect", lock_held=True)
    assert model.inspect_pending(root, lock_held=True) == found
    model.resolve_after_inspection(root, intent_id, lock_held=True)
    assert model.inspect_pending(root, lock_held=True) is None


def test_all_entry_points_require_explicit_lock(root):
    with pytest.raises(model.UnsafeOperationIntent, match="lock"):
        model.begin(root, "metadata", ["file"], lock_held=False)
    with pytest.raises(model.UnsafeOperationIntent, match="lock"):
        model.inspect_pending(root, lock_held=False)
    with pytest.raises(model.UnsafeOperationIntent, match="lock"):
        model.resolve_after_inspection(root, "x", lock_held=False)
    assert not (root / ".library" / model._NAME).exists()


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../b", "a//b", "a/./b", "a\\b"])
def test_unsafe_intent_paths_refused_before_create(root, path):
    with pytest.raises(model.UnsafeOperationIntent):
        model.begin(root, "metadata", [path], lock_held=True)
    assert not (root / ".library" / model._NAME).exists()


@pytest.mark.parametrize("content", [b"", b"{", b"{}", b"[]", b"x" * 65537])
def test_incomplete_or_corrupt_intent_blocks(root, content):
    marker = root / ".library" / model._NAME
    marker.write_bytes(content)
    with pytest.raises(model.UnsafeOperationIntent):
        model.inspect_pending(root, lock_held=True)
    with pytest.raises(model.UnsafeOperationIntent):
        model.begin(root, "metadata", ["file"], lock_held=True)
    assert marker.read_bytes() == content


def test_intent_symlink_and_hardlink_refused(root, tmp_path):
    marker = root / ".library" / model._NAME
    target = tmp_path / "target"
    target.write_bytes(b"protected")
    marker.symlink_to(target)
    with pytest.raises(model.UnsafeOperationIntent):
        model.inspect_pending(root, lock_held=True)
    marker.unlink()
    os.link(target, marker)
    with pytest.raises(model.UnsafeOperationIntent):
        model.inspect_pending(root, lock_held=True)
    assert target.read_bytes() == b"protected"


def test_alias_or_missing_metadata_directory_refused(root):
    alias = root.parent / "archive-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(model.UnsafeOperationIntent):
        model.begin(alias, "metadata", ["file"], lock_held=True)
    (root / ".library").rmdir()
    with pytest.raises(model.UnsafeOperationIntent):
        model.inspect_pending(root, lock_held=True)


def test_create_fsync_order(root, monkeypatch):
    events = []
    fsync = model.os.fsync
    sync_dir = model._sync_dir

    def record_fsync(fd):
        events.append("fsync")
        return fsync(fd)

    def record_dir(path):
        events.append("dir:" + path.name)
        return sync_dir(path)

    monkeypatch.setattr(model.os, "fsync", record_fsync)
    monkeypatch.setattr(model, "_sync_dir", record_dir)
    model.begin(root, "metadata", ["file"], lock_held=True)
    assert events == ["dir:" + root.name, "fsync", "fsync", "dir:.library", "fsync"]


def test_failed_file_fsync_retains_marker_and_does_not_authorize_publication(root, monkeypatch):
    fsync = model.os.fsync
    count = 0

    def fail_second(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("file fsync failed")
        return fsync(fd)

    monkeypatch.setattr(model.os, "fsync", fail_second)
    with pytest.raises(model.UnsafeOperationIntent, match="cannot durably create"):
        model.begin(root, "metadata", ["file"], lock_held=True)
    assert model.inspect_pending(root, lock_held=True) is not None


def test_failed_marker_directory_fsync_retains_marker(root, monkeypatch):
    sync_dir = model._sync_dir

    def failing_directory(path):
        if path.name == ".library":
            raise OSError("directory fsync failed")
        sync_dir(path)

    monkeypatch.setattr(model, "_sync_dir", failing_directory)
    with pytest.raises(model.UnsafeOperationIntent, match="cannot durably create"):
        model.begin(root, "metadata", ["file"], lock_held=True)
    assert model.inspect_pending(root, lock_held=True) is not None


def test_root_sync_failure_precedes_marker_creation(root, monkeypatch):
    def fail(_path):
        raise OSError("root fsync failed")

    monkeypatch.setattr(model, "_sync_dir", fail)
    with pytest.raises(model.UnsafeOperationIntent):
        model.begin(root, "metadata", ["file"], lock_held=True)
    assert model.inspect_pending(root, lock_held=True) is None


def test_resolution_directory_fsync_failure_is_explicitly_uncertain(root, monkeypatch):
    intent_id = model.begin(root, "metadata", ["file"], lock_held=True)

    def fail(_path):
        raise OSError("resolution fsync failed")

    monkeypatch.setattr(model, "_sync_dir", fail)
    with pytest.raises(model.UnsafeOperationIntent, match="durability uncertain"):
        model.resolve_after_inspection(root, intent_id, lock_held=True)
    # unlink occurred: this model cannot enforce an interprocess stop on I/O failure.
    assert not (root / ".library" / model._NAME).exists()


def test_process_exit_after_durable_creation_leaves_pending_marker(root):
    code = (
        "from pathlib import Path; import os,sys; "
        "from tests import tag_operation_intent_model as m; "
        "from suno_dl.archive_lock import _exclusive_archive_sync; "
        "root=Path(sys.argv[1]); "
        "lock=_exclusive_archive_sync(root); lock.__enter__(); "
        "m.begin(root, 'metadata', ['file'], lock_held=True); os._exit(42)"
    )
    process = subprocess.run([sys.executable, "-c", code, str(root)], check=False)
    assert process.returncode == 42
    assert model.inspect_pending(root, lock_held=True)["paths"] == ["file"]


def test_truncated_creation_after_process_exit_blocks(root):
    marker = root / ".library" / model._NAME
    code = (
        "import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); "
        "os.write(fd, b'{{'); os.fsync(fd); os._exit(43)"
    )
    process = subprocess.run([sys.executable, "-c", code, str(marker)], check=False)
    assert process.returncode == 43
    with pytest.raises(model.UnsafeOperationIntent):
        model.inspect_pending(root, lock_held=True)


def test_other_platform_refused(root, monkeypatch):
    monkeypatch.setattr(model.sys, "platform", "win32")
    with pytest.raises(model.UnsafeOperationIntent, match="Linux"):
        model.inspect_pending(root, lock_held=True)


@pytest.mark.parametrize("call", [1, 2, 3])
@pytest.mark.parametrize("when", ["before", "after"])
def test_process_exit_at_each_creation_fsync_boundary(root, call, when):
    code = """
import os, sys
from pathlib import Path
from suno_dl.archive_lock import _exclusive_archive_sync
from tests import tag_operation_intent_model as m
root = Path(sys.argv[1])
which, when = int(sys.argv[2]), sys.argv[3]
real_fsync = os.fsync
count = 0
def interrupted(fd):
    global count
    count += 1
    if count == which and when == 'before':
        os._exit(44)
    real_fsync(fd)
    if count == which and when == 'after':
        os._exit(44)
with _exclusive_archive_sync(root):
    # Inject only into intent creation, after the real lock is established.
    m.os.fsync = interrupted
    m.begin(root, 'metadata', ['file'], lock_held=True)
os._exit(45)
"""
    process = subprocess.run([sys.executable, "-c", code, str(root), str(call), when], check=False)
    assert process.returncode == 44
    # Abrupt termination also releases the OS lock, so the next caller can
    # inspect under the same Trackie lock. A power loss is not simulated.
    from suno_dl.archive_lock import _exclusive_archive_sync

    with _exclusive_archive_sync(root):
        if call == 1:
            assert model.inspect_pending(root, lock_held=True) is None
        else:
            assert model.inspect_pending(root, lock_held=True) is not None
            with pytest.raises(model.UnsafeOperationIntent):
                model.begin(root, "metadata", ["file"], lock_held=True)
    assert not (root / "file").exists()
