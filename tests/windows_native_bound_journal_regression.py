"""Disposable native tests for the unintegrated bound-journal observer.

This only tests process interruptions and observed sharing on local NTFS.
It does not test power loss, coordinated rollback or production recovery.
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

import windows_bound_journal_model as bound
import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as experiment


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeBoundJournalExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="trackie-w-bound-")
        self.addCleanup(self.temp.cleanup)
        self.api = native.NativeWin32(experimental=True)
        handle = self.api.open(self.temp.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(native._canonical_name(self.api.final_path(handle)))
        finally:
            self.api.close(handle)
        _volume, kind, fs = self.api.volume(self.root.anchor)
        if kind != native._DRIVE_FIXED or fs != "NTFS":
            self.skipTest("requires fixed NTFS")
        self.env = dict(os.environ)
        if not self.env.get("TRACKIE_NATIVE_MODEL"):
            self.env["TRACKIE_NATIVE_MODEL"] = base64.b64encode(
                pathlib.Path(native.__file__).read_bytes()
            ).decode()
        if not self.env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
            lock_path = (
                pathlib.Path(native.__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            )
            self.env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(
                lock_path.read_bytes()
            ).decode()

    def _fixture(self, name: str) -> tuple[pathlib.Path, bound.ExternalBinding]:
        root = self.root / name
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        identifier = uuid.uuid4().hex
        path = root / "journal.bin"
        with path.open("xb", buffering=0) as stream:
            stream.write(experiment.MAGIC + b"ID=" + identifier.encode("ascii") + b"\n")
            os.fsync(stream.fileno())
        handle = self.api.open(str(path), directory=False, writable=False)
        try:
            info = self.api.info(handle)
        finally:
            self.api.close(handle)
        return root, bound.ExternalBinding(
            info.volume_serial, info.index_high, info.index_low, identifier
        )

    def _stop_at(self, root: pathlib.Path, phase: str) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", experiment._CHILD, phase, str(root)],
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

    def test_interrupted_records_are_classified_or_refused(self) -> None:
        for phase in ("partial-intent", "intent", "publication", "partial-complete", "complete"):
            with self.subTest(phase=phase):
                root, binding = self._fixture(phase)
                self._stop_at(root, phase)
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                with bound.BoundJournalSession(root, binding) as session:
                    if phase.startswith("partial"):
                        with self.assertRaises(bound.BindingError):
                            session.classify()
                    else:
                        self.assertEqual(
                            session.classify(),
                            {
                                "intent": "pending-stage",
                                "publication": "pending-target",
                                "complete": "complete",
                            }[phase],
                        )
                self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_wrong_external_ids_and_replaced_journal_are_refused(self) -> None:
        root, binding = self._fixture("identity")
        with (
            self.assertRaises(bound.BindingError),
            bound.BoundJournalSession(
                root,
                bound.ExternalBinding(
                    binding.volume_serial, binding.index_high, binding.index_low, "0" * 32
                ),
            ),
        ):
            pass
        with (
            self.assertRaises(bound.BindingError),
            bound.BoundJournalSession(
                root,
                bound.ExternalBinding(
                    binding.volume_serial,
                    binding.index_high,
                    binding.index_low ^ 1,
                    binding.journal_id,
                ),
            ),
        ):
            pass
        journal_path = root / "journal.bin"
        original = self.root / "original-journal"
        journal_path.rename(original)
        journal_path.write_bytes(original.read_bytes())
        with self.assertRaises(bound.BindingError), bound.BoundJournalSession(root, binding):
            pass
        self.assertEqual(journal_path.read_bytes(), original.read_bytes())

    def test_live_capability_blocks_lock_and_journal_replacement(self) -> None:
        root, binding = self._fixture("held")
        lock_code = r"""
import base64, os, pathlib, sys, types
m=types.ModuleType("trackie_lock");sys.modules[m.__name__]=m
source=base64.b64decode(os.environ["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode()
exec(compile(source, "lock.py", "exec"),m.__dict__)
m._WAIT_SEC=0.25;m._POLL_SEC=0.01
try:
    with m._exclusive_archive_sync(pathlib.Path(sys.argv[1])): print("ACQUIRED")
except TimeoutError: print("BLOCKED")
"""
        with bound.BoundJournalSession(root, binding) as session:
            self.assertEqual(session.classify(), "empty")
            with self.assertRaises(bound.BindingError):
                session.__enter__()
            self.assertEqual(session.classify(), "empty")
            child = subprocess.run(
                [sys.executable, "-c", lock_code, str(root)],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=10,
            )
            self.assertEqual((child.returncode, child.stdout.strip()), (0, "BLOCKED"))
            renamed = self.root / "journal-moved"
            move = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys;os.rename(sys.argv[1],sys.argv[2])",
                    str(root / "journal.bin"),
                    str(renamed),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(move.returncode, 0)
            self.assertFalse(renamed.exists())
            moved_root = self.root / "archive-moved"
            root_move = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys;os.rename(sys.argv[1],sys.argv[2])",
                    str(root),
                    str(moved_root),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(root_move.returncode, 0)
            self.assertTrue(root.is_dir())
            self.assertFalse(moved_root.exists())
            write_attempt = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys;open(sys.argv[1], 'ab').write(b'x')",
                    str(root / "journal.bin"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(write_attempt.returncode, 0)
            self.assertEqual(session.classify(), "empty")
        (root / "journal.bin").rename(renamed)
        self.assertTrue(renamed.exists())
        root.rename(moved_root)
        self.assertTrue(moved_root.is_dir())
        with self.assertRaises(bound.BindingError):
            session.__enter__()

    def test_held_ancestor_directory_cannot_be_replaced(self) -> None:
        parent = self.root / "ancestor"
        parent.mkdir()
        root, binding = self._fixture("ancestor/nested")
        moved = self.root / "ancestor-moved"
        command = "import os,sys;os.rename(sys.argv[1],sys.argv[2])"
        with bound.BoundJournalSession(root, binding) as session:
            child = subprocess.run(
                [sys.executable, "-c", command, str(parent), str(moved)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(child.returncode, 0)
            self.assertTrue(parent.is_dir())
            self.assertFalse(moved.exists())
            self.assertEqual(session.classify(), "empty")
        parent.rename(moved)
        self.assertTrue((moved / "nested").is_dir())

    def test_unexplained_member_and_closed_session_are_refused(self) -> None:
        root, binding = self._fixture("unexplained")
        extra = root / "unexplained.m4a"
        extra.write_bytes(b"existing bytes")
        with (
            bound.BoundJournalSession(root, binding) as session,
            self.assertRaises(bound.BindingError),
        ):
            session.classify()
        self.assertEqual(extra.read_bytes(), b"existing bytes")
        with self.assertRaises(bound.BindingError):
            session.classify()

    def test_invalid_binding_and_hardlinked_media_are_refused(self) -> None:
        root, binding = self._fixture("invalid-binding")
        for invalid in (
            bound.ExternalBinding(binding.volume_serial, 0, 0, binding.journal_id),
            bound.ExternalBinding(
                binding.volume_serial, binding.index_high, binding.index_low, "f" * 31
            ),
            bound.ExternalBinding(
                binding.volume_serial ^ 1, binding.index_high, binding.index_low, binding.journal_id
            ),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(bound.BindingError),
                bound.BoundJournalSession(root, invalid),
            ):
                pass
        self._stop_at(root, "intent")
        stage = root / "stage.bin"
        os.link(stage, self.root / "stage-hardlink.bin")
        with (
            bound.BoundJournalSession(root, binding) as session,
            self.assertRaises(bound.BindingError),
        ):
            session.classify()
        self.assertEqual(stage.read_bytes(), b"persistent-journal-sentinel")

    def test_matching_inode_reparse_media_is_refused(self) -> None:
        root, binding = self._fixture("reparse")
        self._stop_at(root, "intent")
        original, link = self.root / "original-stage", root / "stage.bin"
        link.rename(original)
        try:
            os.symlink(original, link)
        except OSError as exc:
            self.skipTest(f"disposable symlink unavailable: {exc}")
        with (
            bound.BoundJournalSession(root, binding) as session,
            self.assertRaises(bound.BindingError),
        ):
            session.classify()
        self.assertEqual(original.read_bytes(), b"persistent-journal-sentinel")

    def test_whole_completion_rollback_needs_external_monotonic_witness(self) -> None:
        root, binding = self._fixture("rollback")
        self._stop_at(root, "complete")
        path = root / "journal.bin"
        with bound.BoundJournalSession(root, binding) as session:
            self.assertEqual(session.classify(), "complete")
        lines = path.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 4)
        with path.open("r+b", buffering=0) as stream:
            stream.seek(sum(len(line) for line in lines[:3]))
            stream.truncate()
            os.fsync(stream.fileno())
        with bound.BoundJournalSession(root, binding) as session:
            self.assertEqual(session.classify(), "pending-target")


if __name__ == "__main__":
    unittest.main(verbosity=2)
