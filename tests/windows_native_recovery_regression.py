"""Test-only real Windows process interruption and fail-closed recovery.

No power-loss, sidecar, multiple-operation or production-safety claims.
Run directly with standard-library Python on a disposable NTFS test host.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import windows_filesystem_adapter_model as native
import windows_recovery_model as recovery

_CHILD = r"""
import base64, os, pathlib, sys, time, types
for module_name, key in (("windows_filesystem_adapter_model", "TRACKIE_NATIVE_MODEL"),
                         ("windows_recovery_model", "TRACKIE_RECOVERY_MODEL")):
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    source = base64.b64decode(os.environ[key]).decode("utf-8")
    exec(compile(source, module_name + ".py", "exec"), module.__dict__)
import windows_recovery_model as r
phase, root = sys.argv[1], pathlib.Path(sys.argv[2])
def pause(state):
    if state == phase:
        print("READY " + phase, flush=True)
        time.sleep(300)
with r.LockedArchive(root) as locked:
    r.start(locked, "target-λ.bin", b"verified sentinel", hook=pause)
"""


_LOCK_CHILD = r"""
import base64, os, pathlib, sys, types
mode, root = sys.argv[1], pathlib.Path(sys.argv[2])
def load(name, key):
    module = types.ModuleType(name)
    sys.modules[name] = module
    source = base64.b64decode(os.environ[key]).decode("utf-8")
    exec(compile(source, name + ".py", "exec"), module.__dict__)
    return module
if mode == "existing":
    lock = load("trackie_existing_lock", "TRACKIE_ARCHIVE_LOCK_MODEL")
    lock._WAIT_SEC, lock._POLL_SEC = 0.25, 0.01
    try:
        with lock._exclusive_archive_sync(root):
            print("ACQUIRED")
    except TimeoutError:
        print("BLOCKED")
else:
    load("windows_filesystem_adapter_model", "TRACKIE_NATIVE_MODEL")
    model = load("windows_recovery_model", "TRACKIE_RECOVERY_MODEL")
    try:
        with model.LockedArchive(root):
            print("ACQUIRED")
    except model.RecoveryError:
        print("BLOCKED")
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeRecoveryExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="trackie-w-recovery-")
        self.addCleanup(self.temporary.cleanup)
        api = native.NativeWin32(experimental=True)
        handle = api.open(self.temporary.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(native._canonical_name(api.final_path(handle)))
        finally:
            api.close(handle)
        _volume, drive_type, filesystem = api.volume(self.root.anchor)
        if drive_type != native._DRIVE_FIXED or filesystem != "NTFS":
            self.skipTest("requires fixed NTFS temporary volume")
        self.env = dict(os.environ)
        if not self.env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
            existing_lock = (
                pathlib.Path(recovery.__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            )
            self.env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(
                existing_lock.read_bytes()
            ).decode()
        for module, key in ((native, "TRACKIE_NATIVE_MODEL"), (recovery, "TRACKIE_RECOVERY_MODEL")):
            if not self.env.get(key):
                self.env[key] = base64.b64encode(
                    pathlib.Path(module.__file__).read_bytes()
                ).decode()

    def _make_root(self, name: str) -> pathlib.Path:
        root = self.root / name
        root.mkdir()
        (root / recovery.LOCK).write_bytes(b"0")
        return root

    def _stop_at(self, root: pathlib.Path, phase: str) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, phase, str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        messages: queue.Queue[str] = queue.Queue()
        assert child.stdout is not None
        reader = threading.Thread(target=lambda: messages.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(messages.get(timeout=20).strip(), "READY " + phase)
            with self.assertRaises(recovery.RecoveryError), recovery.LockedArchive(root):
                self.fail("another process acquired a held lock")
        finally:
            child.terminate()
            child.wait(timeout=10)
            child.stdout.close()

    def test_termination_at_journal_boundaries(self) -> None:
        for phase in ("pending", "stage", "ready", "published", "complete"):
            with self.subTest(phase=phase):
                root = self._make_root(phase)
                self._stop_at(root, phase)
                before = {path.name for path in root.iterdir()}
                with recovery.LockedArchive(root) as locked:
                    if phase in ("pending", "stage"):
                        with self.assertRaises(recovery.RecoveryError):
                            recovery.recover(locked)
                        self.assertEqual({path.name for path in root.iterdir()}, before)
                        self.assertFalse((root / "target-λ.bin").exists())
                    else:
                        self.assertEqual(recovery.recover(locked), "complete")
                        self.assertEqual((root / "target-λ.bin").read_bytes(), b"verified sentinel")
                        self.assertEqual(recovery.recover(locked), "complete")

    def test_rejects_changed_installed_inode_and_duplicate_record(self) -> None:
        root = self._make_root("tamper")
        self._stop_at(root, "published")
        target = root / "target-λ.bin"
        moved = root / "other.bin"
        target.rename(moved)
        target.write_bytes(moved.read_bytes())
        with recovery.LockedArchive(root) as locked, self.assertRaises(recovery.RecoveryError):
            recovery.recover(locked)
        self.assertEqual(target.read_bytes(), b"verified sentinel")
        self.assertEqual(moved.read_bytes(), b"verified sentinel")
        ready = root / recovery.READY
        data = json.loads(ready.read_bytes())
        ready.write_text(json.dumps(data)[:-1] + ',"id":"' + data["id"] + '"}')
        with (
            recovery.LockedArchive(root) as locked,
            self.assertRaisesRegex(recovery.RecoveryError, "Duplicate journal key"),
        ):
            recovery.recover(locked)

    def test_refuses_stage_conflicts_tampering_and_partial_records(self) -> None:
        for mode in ("collision", "bytes", "hardlink", "partial", "pending-drift"):
            with self.subTest(mode=mode):
                root = self._make_root("refuse-" + mode)
                self._stop_at(root, "ready")
                pending = json.loads((root / recovery.PENDING).read_bytes())
                stage = root / pending["stage"]
                target = root / pending["target"]
                if mode == "collision":
                    target.write_bytes(b"unrelated target")
                elif mode == "bytes":
                    stage.write_bytes(b"modified payload")
                elif mode == "hardlink":
                    os.link(stage, root / "alias.bin")
                elif mode == "partial":
                    (root / recovery.READY).write_bytes(b'{"version":1')
                else:
                    pending["target"] = "different-target.bin"
                    (root / recovery.PENDING).write_text(json.dumps(pending))
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                with (
                    recovery.LockedArchive(root) as locked,
                    self.assertRaises(recovery.RecoveryError),
                ):
                    recovery.recover(locked)
                self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    def test_partial_completion_record_preserves_published_target(self) -> None:
        root = self._make_root("partial-complete")
        self._stop_at(root, "complete")
        target = root / "target-λ.bin"
        (root / recovery.COMPLETE).write_bytes(b'{"version":1')
        with recovery.LockedArchive(root) as locked, self.assertRaises(recovery.RecoveryError):
            recovery.recover(locked)
        self.assertEqual(target.read_bytes(), b"verified sentinel")

    def _try_existing_lock_child(self, mode: str, root: pathlib.Path) -> str:
        child = subprocess.run(
            [sys.executable, "-c", _LOCK_CHILD, mode, str(root)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        return child.stdout.strip()

    def test_cross_process_existing_archive_lock_interoperability(self) -> None:
        root = self._make_root("interop")
        existing = types.ModuleType("trackie_existing_lock_parent")
        sys.modules[existing.__name__] = existing
        code = base64.b64decode(self.env["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
        exec(compile(code, existing.__name__ + ".py", "exec"), existing.__dict__)  # noqa: S102
        existing._WAIT_SEC, existing._POLL_SEC = 0.25, 0.01
        with recovery.LockedArchive(root):
            self.assertEqual(self._try_existing_lock_child("existing", root), "BLOCKED")
        self.assertEqual(self._try_existing_lock_child("existing", root), "ACQUIRED")
        with existing._exclusive_archive_sync(root):
            self.assertEqual(self._try_existing_lock_child("research", root), "BLOCKED")
        self.assertEqual(self._try_existing_lock_child("research", root), "ACQUIRED")

    def test_same_process_unlock_is_not_attested(self) -> None:
        import msvcrt

        root = self._make_root("same-process-unlock")
        with recovery.LockedArchive(root) as locked:
            locked._unlock()
            try:
                # A live descriptor and matching identity do not prove the
                # underlying byte-range lock was not explicitly unlocked.
                locked.verify()
                self.assertEqual(self._try_existing_lock_child("existing", root), "ACQUIRED")
            finally:
                locked.file.seek(0)
                msvcrt.locking(locked.file.fileno(), msvcrt.LK_NBLCK, 1)

    def test_completion_rejects_ready_record_drift(self) -> None:
        root = self._make_root("ready-drift")
        self._stop_at(root, "complete")
        target = root / "target-λ.bin"
        ready_path = root / recovery.READY
        ready = json.loads(ready_path.read_bytes())
        ready["identity"][1] += 1
        ready_path.write_text(json.dumps(ready))
        with recovery.LockedArchive(root) as locked, self.assertRaises(recovery.RecoveryError):
            recovery.recover(locked)
        self.assertEqual(target.read_bytes(), b"verified sentinel")

    def test_failed_ready_fsync_cannot_publish_until_reflush_succeeds(self) -> None:
        root = self._make_root("ready-fsync")
        with recovery.LockedArchive(root) as locked:
            original = recovery.os.fsync
            calls = [0]

            def fail_ready(fd):
                calls[0] += 1
                if calls[0] == 3:
                    raise OSError("injected ready fsync failure")
                return original(fd)

            with (
                patch.object(recovery.os, "fsync", side_effect=fail_ready),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.start(locked, "target.bin", b"sentinel")
            self.assertTrue((root / recovery.READY).exists())
            with (
                patch.object(recovery.os, "fsync", side_effect=OSError("still failing")),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.recover(locked)
            self.assertFalse((root / "target.bin").exists())
            self.assertEqual(recovery.recover(locked), "complete")
            self.assertEqual((root / "target.bin").read_bytes(), b"sentinel")

    def test_failed_completion_fsync_needs_successful_reflush(self) -> None:
        root = self._make_root("complete-fsync")
        with recovery.LockedArchive(root) as locked:
            original = recovery._write_record

            def fail_complete(api, directory, name, record):
                if name == recovery.COMPLETE:
                    with patch.object(recovery.os, "fsync", side_effect=OSError("injected")):
                        return original(api, directory, name, record)
                return original(api, directory, name, record)

            with (
                patch.object(recovery, "_write_record", side_effect=fail_complete),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.start(locked, "target.bin", b"sentinel")
            self.assertEqual((root / "target.bin").read_bytes(), b"sentinel")
            self.assertTrue((root / recovery.COMPLETE).exists())
            with (
                patch.object(recovery.os, "fsync", side_effect=OSError("still failing")),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.recover(locked)
            self.assertEqual(recovery.recover(locked), "complete")

    def test_failed_ready_directory_flush_needs_successful_reflush(self) -> None:
        root = self._make_root("ready-dir-flush")
        with recovery.LockedArchive(root) as locked:
            original = recovery._flush_directory

            def fail_ready_directory(api, directory):
                if (directory / recovery.READY).exists():
                    raise OSError("injected directory flush failure")
                return original(api, directory)

            with (
                patch.object(recovery, "_flush_directory", side_effect=fail_ready_directory),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.start(locked, "target.bin", b"sentinel")
            self.assertFalse((root / "target.bin").exists())
            with (
                patch.object(recovery, "_flush_directory", side_effect=OSError("still failing")),
                self.assertRaises(OSError),
            ):
                recovery.recover(locked)
            self.assertFalse((root / "target.bin").exists())
            self.assertEqual(recovery.recover(locked), "complete")

    def test_rename_error_before_or_after_publication_recovers_by_identity(self) -> None:
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                root = self._make_root("rename-error-" + mode)
                with recovery.LockedArchive(root) as locked:
                    original = locked.api.rename_no_replace

                    def fail_rename(handle, destination, mode=mode, original=original):
                        if mode == "after":
                            original(handle, destination)
                        raise native.AdapterError("injected uncertain rename")

                    with (
                        patch.object(locked.api, "rename_no_replace", side_effect=fail_rename),
                        self.assertRaises(native.AdapterError),
                    ):
                        recovery.start(locked, "target.bin", b"sentinel")
                    self.assertEqual((root / "target.bin").exists(), mode == "after")
                    self.assertEqual(recovery.recover(locked), "complete")
                    self.assertEqual((root / "target.bin").read_bytes(), b"sentinel")

    def test_held_archive_directory_cannot_be_renamed_by_another_process(self) -> None:
        root = self._make_root("held-path")
        moved = self.root / "moved-path"
        command = "import os,sys; os.rename(sys.argv[1], sys.argv[2])"
        with recovery.LockedArchive(root):
            child = subprocess.run(
                [sys.executable, "-c", command, str(root), str(moved)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(child.returncode, 0)
            self.assertTrue(root.is_dir())
            self.assertFalse(moved.exists())
        root.rename(moved)
        self.assertTrue(moved.is_dir())

    def test_closed_lock_capability_is_refused(self) -> None:
        root = self._make_root("closed")
        with recovery.LockedArchive(root) as locked:
            locked.verify()
        with self.assertRaises(recovery.RecoveryError):
            recovery.start(locked, "target.bin", b"fixture")
        self.assertEqual({path.name for path in root.iterdir()}, {recovery.LOCK})


if __name__ == "__main__":
    unittest.main(verbosity=2)
