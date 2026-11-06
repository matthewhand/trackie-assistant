"""Disposable native tests for unintegrated bound-journal publication research.

A process kill is not power loss. No production, sidecar, overwrite, rollback
witness, malicious same-process or noncooperating-writer safety is established.
"""

from __future__ import annotations

import base64
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import windows_bound_publish_model as publish
import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as journal

_CHILD = r"""
import base64, os, pathlib, sys, time, types
for name, key in (("windows_filesystem_adapter_model", "TRACKIE_NATIVE_MODEL"),
                  ("windows_recovery_model", "TRACKIE_RECOVERY_MODEL"),
                  ("windows_persistent_journal_experiment", "TRACKIE_JOURNAL_MODEL"),
                  ("windows_bound_publish_model", "TRACKIE_PUBLISH_MODEL")):
    module = types.ModuleType(name); sys.modules[name] = module
    source = base64.b64decode(os.environ[key]).decode("utf-8")
    exec(compile(source, name + ".py", "exec"), module.__dict__)
import windows_bound_publish_model as p
phase, root, volume, high, low, identifier = sys.argv[1:]
binding = p.ExternalBinding(int(volume), int(high), int(low), identifier)
def pause(state):
    if state == phase:
        print("READY " + phase, flush=True); time.sleep(300)
with p.BoundPublisher(pathlib.Path(root), binding) as session:
    session.begin(b"bound publication sentinel", hook=pause)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeBoundPublishExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="trackie-w-publish-")
        self.addCleanup(self.temp.cleanup)
        self.api = native.NativeWin32(experimental=True)
        h = self.api.open(self.temp.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(native._canonical_name(self.api.final_path(h)))
        finally:
            self.api.close(h)
        _volume, kind, fs = self.api.volume(self.root.anchor)
        if kind != native._DRIVE_FIXED or fs != "NTFS":
            self.skipTest("requires fixed NTFS")
        self.env = dict(os.environ)
        modules = (
            (native, "TRACKIE_NATIVE_MODEL"),
            (journal, "TRACKIE_JOURNAL_MODEL"),
            (publish, "TRACKIE_PUBLISH_MODEL"),
        )
        for module, key in modules:
            if not self.env.get(key):
                self.env[key] = base64.b64encode(
                    pathlib.Path(module.__file__).read_bytes()
                ).decode()
        if not self.env.get("TRACKIE_RECOVERY_MODEL"):
            recovery_path = pathlib.Path(publish.__file__).with_name("windows_recovery_model.py")
            self.env["TRACKIE_RECOVERY_MODEL"] = base64.b64encode(
                recovery_path.read_bytes()
            ).decode()

        if not self.env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
            lock_path = (
                pathlib.Path(native.__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            )
            self.env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(
                lock_path.read_bytes()
            ).decode()

    def _fixture(self, name: str) -> tuple[pathlib.Path, publish.ExternalBinding]:
        root = self.root / name
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        identifier = uuid.uuid4().hex
        path = root / "journal.bin"
        with path.open("xb", buffering=0) as stream:
            stream.write(journal.MAGIC + b"ID=" + identifier.encode() + b"\n")
            os.fsync(stream.fileno())
        h = self.api.open(str(path), directory=False, writable=False)
        try:
            info = self.api.info(h)
        finally:
            self.api.close(h)
        return root, publish.ExternalBinding(
            info.volume_serial, info.index_high, info.index_low, identifier
        )

    def _stop_at(self, root: pathlib.Path, binding: publish.ExternalBinding, phase: str) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                phase,
                str(root),
                str(binding.volume_serial),
                str(binding.index_high),
                str(binding.index_low),
                binding.journal_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        output: queue.Queue[str] = queue.Queue()
        assert child.stdout is not None
        stdout = child.stdout
        threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(output.get(timeout=20).strip(), "READY " + phase)
        finally:
            child.terminate()
            child.wait(timeout=10)
            stdout.close()

    def test_kill_boundaries_fail_closed_or_recover(self) -> None:
        blocked = {
            "stage_created",
            "stage_written",
            "stage_flushed",
            "pending_partial",
            "complete_partial",
        }
        phases = (
            *sorted(blocked),
            "pending_written",
            "pending_flushed",
            "published",
            "complete_written",
            "complete_flushed",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                root, binding = self._fixture(phase)
                self._stop_at(root, binding, phase)
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                with publish.BoundPublisher(root, binding) as session:
                    if phase in blocked:
                        with self.assertRaises(publish.BindingError):
                            session.recover()
                    else:
                        self.assertEqual(session.recover(), "complete")
                        self.assertEqual(session.recover(), "complete")
                        self.assertEqual(
                            (root / "target.bin").read_bytes(), b"bound publication sentinel"
                        )
                if phase in blocked:
                    self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_collision_or_changed_stage_fails_without_overwrite(self) -> None:
        for mode in ("collision", "identity"):
            with self.subTest(mode=mode):
                root, binding = self._fixture(mode)
                self._stop_at(root, binding, "pending_flushed")
                stage, target = root / "stage.bin", root / "target.bin"
                if mode == "collision":
                    target.write_bytes(b"unrelated target")
                else:
                    saved = self.root / "saved-original"
                    stage.rename(saved)
                    stage.write_bytes(saved.read_bytes())
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                with (
                    publish.BoundPublisher(root, binding) as session,
                    self.assertRaises(publish.BindingError),
                ):
                    session.recover()
                self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_persistent_fsync_failure_prevents_publication(self) -> None:
        root, binding = self._fixture("fsync-before")
        self._stop_at(root, binding, "pending_flushed")
        with publish.BoundPublisher(root, binding) as session:
            with (
                patch.object(publish.os, "fsync", side_effect=OSError("injected")),
                self.assertRaises(publish.BindingError),
            ):
                session.recover()
            self.assertFalse((root / "target.bin").exists())
            self.assertEqual(session.recover(), "complete")

    def test_target_flush_failure_keeps_pending_for_recovery(self) -> None:
        root, binding = self._fixture("fsync-after")
        self._stop_at(root, binding, "pending_flushed")
        with publish.BoundPublisher(root, binding) as session:
            real = publish.os.fsync
            calls = [0]

            def fail_target(fd):
                calls[0] += 1
                if calls[0] == 3:
                    raise OSError("injected target flush failure")
                return real(fd)

            with (
                patch.object(publish.os, "fsync", side_effect=fail_target),
                self.assertRaises(publish.BindingError),
            ):
                session.recover()
            self.assertEqual((root / "target.bin").read_bytes(), b"bound publication sentinel")
            self.assertEqual(session.classify(), "pending-target")
            self.assertEqual(session.recover(), "complete")

    def test_live_session_blocks_existing_lock_and_path_replacement(self) -> None:
        root, binding = self._fixture("held")
        lock_code = r"""
import base64, os, pathlib, sys, types
m=types.ModuleType("trackie_lock"); sys.modules[m.__name__]=m
source=base64.b64decode(os.environ["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode()
exec(compile(source, "lock.py", "exec"), m.__dict__)
m._WAIT_SEC=0.25; m._POLL_SEC=0.01
try:
    with m._exclusive_archive_sync(pathlib.Path(sys.argv[1])): print("ACQUIRED")
except TimeoutError: print("BLOCKED")
"""
        journal_path = root / "journal.bin"
        moved_file, moved_root = self.root / "moved-journal", self.root / "moved-root"
        session = publish.BoundPublisher(root, binding)
        with session:
            self.assertEqual(session.classify(), "empty")
            child = subprocess.run(
                [sys.executable, "-c", lock_code, str(root)],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=10,
            )
            self.assertEqual((child.returncode, child.stdout.strip()), (0, "BLOCKED"))
            for source, destination in ((journal_path, moved_file), (root, moved_root)):
                move = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys;os.rename(sys.argv[1],sys.argv[2])",
                        str(source),
                        str(destination),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(move.returncode, 0)
                self.assertFalse(destination.exists())
            write_attempt = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys;open(sys.argv[1], 'ab').write(b'x')",
                    str(journal_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(write_attempt.returncode, 0)
            with self.assertRaises(publish.BindingError):
                session.__enter__()
            self.assertEqual(session.classify(), "empty")
        journal_path.rename(moved_file)
        root.rename(moved_root)
        self.assertTrue(moved_file.exists())
        self.assertTrue(moved_root.is_dir())

    def test_wrong_binding_and_replaced_journal_are_refused_before_writes(self) -> None:
        root, binding = self._fixture("journal-binding")
        wrong = publish.ExternalBinding(
            binding.volume_serial, binding.index_high, binding.index_low, "0" * 32
        )
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        with self.assertRaises(publish.BindingError), publish.BoundPublisher(root, wrong):
            pass
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
        journal_path = root / "journal.bin"
        original = self.root / "saved-journal"
        journal_path.rename(original)
        journal_path.write_bytes(original.read_bytes())
        with self.assertRaises(publish.BindingError), publish.BoundPublisher(root, binding):
            pass
        self.assertEqual(journal_path.read_bytes(), original.read_bytes())

    def test_complete_fsync_error_requires_reflush_before_acknowledgement(self) -> None:
        root, binding = self._fixture("complete-fsync")
        with publish.BoundPublisher(root, binding) as session:
            real = publish.os.fsync
            calls = [0]

            def fail_complete(fd):
                calls[0] += 1
                if calls[0] == 6:
                    raise OSError("injected completion fsync failure")
                return real(fd)

            with (
                patch.object(publish.os, "fsync", side_effect=fail_complete),
                self.assertRaises(publish.BindingError),
            ):
                session.begin(b"sentinel")
            self.assertEqual(session.classify(), "completion-frame-visible")
            with (
                patch.object(publish.os, "fsync", side_effect=OSError("still failing")),
                self.assertRaises(publish.BindingError),
            ):
                session.recover()
            self.assertEqual((root / "target.bin").read_bytes(), b"sentinel")
            self.assertEqual(session.recover(), "complete")

    def test_empty_payload_and_failed_stage_fsync(self) -> None:
        root, binding = self._fixture("empty-payload")
        with publish.BoundPublisher(root, binding) as session:
            self.assertEqual(session.begin(b""), "complete")
            self.assertEqual((root / "target.bin").read_bytes(), b"")
        root, binding = self._fixture("stage-fsync")
        with publish.BoundPublisher(root, binding) as session:
            with (
                patch.object(publish.os, "fsync", side_effect=OSError("injected")),
                self.assertRaises(publish.BindingError),
            ):
                session.begin(b"unchanged sentinel")
            with self.assertRaises(publish.BindingError):
                session.recover()
            self.assertEqual((root / "stage.bin").read_bytes(), b"unchanged sentinel")
            self.assertFalse((root / "target.bin").exists())

    def test_ambiguous_rename_error_reconciles_target_identity(self) -> None:
        root, binding = self._fixture("rename-after")
        self._stop_at(root, binding, "pending_flushed")
        with publish.BoundPublisher(root, binding) as session:
            original = native.Adapter.publish

            def fail_after(adapter, *args, **kwargs):
                original(adapter, *args, **kwargs)
                raise native.AdapterError("injected after rename")

            with (
                patch.object(native.Adapter, "publish", new=fail_after),
                self.assertRaises(native.AdapterError),
            ):
                session.recover()
            self.assertEqual(session.classify(), "pending-target")
            self.assertEqual(session.recover(), "complete")
            self.assertEqual((root / "target.bin").read_bytes(), b"bound publication sentinel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
