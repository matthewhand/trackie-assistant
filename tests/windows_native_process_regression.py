"""Disposable Windows process-boundary research; not a recovery protocol.

Run directly on a test-only Windows host. This does not test power loss,
firmware, uncooperative writers, or prove staging ownership and lock attestation.
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
from typing import Any

import windows_filesystem_adapter_model as model

_CHILD = r"""
import base64, msvcrt, os, sys, time, types
m = types.ModuleType("trackie_child_model")
sys.modules[m.__name__] = m
exec(compile(base64.b64decode(os.environ["TRACKIE_TEST_MODEL"]).decode("utf-8"),
             m.__name__ + ".py", "exec"), m.__dict__)
mode, source, target, lock = sys.argv[1:]
f = open(lock, "r+b", buffering=0)
f.seek(0)
msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
class Paused(m.NativeWin32):
    def flush(self, handle):
        super().flush(handle)
        if mode == "before":
            print("READY before", flush=True)
            time.sleep(300)
    def rename_no_replace(self, handle, destination):
        super().rename_no_replace(handle, destination)
        if mode == "after":
            print("READY after", flush=True)
            time.sleep(300)
adapter = m.Adapter(Paused(experimental=True), archive_lock_held=True)
adapter.publish(source, target, archive_lock_held=True, staged_file_owned=True)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeProcessRegression(unittest.TestCase):
    def setUp(self) -> None:
        import msvcrt

        self.msvcrt: Any = msvcrt
        self.temporary = tempfile.TemporaryDirectory(prefix="trackie-win-process-")
        self.addCleanup(self.temporary.cleanup)
        self.api = model.NativeWin32(experimental=True)
        handle = self.api.open(self.temporary.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(model._canonical_name(self.api.final_path(handle)))
        finally:
            self.api.close(handle)
        _root, drive_type, filesystem = self.api.volume(self.root.anchor)
        if drive_type != model._DRIVE_FIXED or filesystem != "NTFS":
            self.skipTest("requires a fixed NTFS temporary volume")
        source = os.environ.get("TRACKIE_TEST_MODEL")
        if not source:
            source = base64.b64encode(pathlib.Path(model.__file__).read_bytes()).decode("ascii")
        self.child_env = dict(os.environ, TRACKIE_TEST_MODEL=source)

    def _lock(self, stream) -> None:
        stream.seek(0)
        self.msvcrt.locking(stream.fileno(), self.msvcrt.LK_NBLCK, 1)

    def _unlock(self, stream) -> None:
        stream.seek(0)
        self.msvcrt.locking(stream.fileno(), self.msvcrt.LK_UNLCK, 1)

    def test_two_call_lock_ownership_probe_has_a_real_race(self) -> None:
        lock = self.root / "race-lock"
        lock.write_bytes(b"0")
        code = (
            "import msvcrt,sys,time\n"
            "f=open(sys.argv[1], 'r+b', buffering=0)\n"
            "f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "print('HELD', flush=True); sys.stdin.readline()\n"
            "f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)\n"
            "print('RELEASED', flush=True); time.sleep(300)\n"
        )
        with lock.open("r+b", buffering=0) as old_owner:
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(lock)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert child.stdin is not None and child.stdout is not None
            stdout = child.stdout

            def read_line() -> str:
                output: queue.Queue[str] = queue.Queue()
                threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
                return output.get(timeout=20).strip()

            try:
                self.assertEqual(read_line(), "HELD")
                with lock.open("r+b", buffering=0) as probe, self.assertRaises(OSError):
                    self._lock(probe)
                child.stdin.write("release\n")
                child.stdin.flush()
                self.assertEqual(read_line(), "RELEASED")
                old_owner.seek(0)
                self.assertEqual(old_owner.read(1), b"0")
                # The blocked probe and successful owner read did not prove
                # ownership: the other process released between those calls.
                with lock.open("r+b", buffering=0) as witness:
                    self._lock(witness)
                    self._unlock(witness)
            finally:
                child.terminate()
                child.wait(timeout=10)
                child.stdin.close()
                child.stdout.close()

    def test_termination_on_both_sides_of_publication(self) -> None:
        for mode in ("before", "after"):
            with self.subTest(boundary=mode):
                folder = self.root / mode
                folder.mkdir()
                source, target, lock = (folder / name for name in ("stage", "target", "lock"))
                source.write_bytes(b"controlled sentinel")
                lock.write_bytes(b"0")
                child = subprocess.Popen(
                    [sys.executable, "-c", _CHILD, mode, str(source), str(target), str(lock)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=self.child_env,
                )
                messages: queue.Queue[str] = queue.Queue()
                assert child.stdout is not None
                threading.Thread(
                    target=lambda messages=messages, stream=child.stdout: messages.put(
                        stream.readline()
                    ),
                    daemon=True,
                ).start()
                try:
                    self.assertEqual(messages.get(timeout=20).strip(), "READY " + mode)
                    with lock.open("r+b", buffering=0) as contender, self.assertRaises(OSError):
                        self._lock(contender)
                finally:
                    child.terminate()
                    child.wait(timeout=10)
                    child.stdout.close()
                with lock.open("r+b", buffering=0) as acquired:
                    self._lock(acquired)
                    try:
                        adapter = model.Adapter(self.api, archive_lock_held=True)
                        if mode == "before":
                            self.assertEqual(source.read_bytes(), b"controlled sentinel")
                            self.assertFalse(target.exists())
                            adapter.publish(
                                str(source),
                                str(target),
                                archive_lock_held=True,
                                staged_file_owned=True,
                            )
                        else:
                            self.assertFalse(source.exists())
                            with self.assertRaises(model.AdapterError):
                                adapter.publish(
                                    str(source),
                                    str(target),
                                    archive_lock_held=True,
                                    staged_file_owned=True,
                                )
                        self.assertEqual(target.read_bytes(), b"controlled sentinel")
                    finally:
                        self._unlock(acquired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
