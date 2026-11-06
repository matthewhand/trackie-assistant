"""Disposable Windows test of a documented byte-range lock limitation.

This is an uncooperative-writer demonstration, not a bypass of coordination:
the archive lock remains held, but a mapped view can modify the locked byte.
It does not test power-loss durability, authentication or production recovery.
"""

from __future__ import annotations

import base64
import mmap
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

import windows_filesystem_adapter_model as native

_CHILD = r"""
import base64, os, pathlib, sys, time, types
m=types.ModuleType("trackie_lock");sys.modules[m.__name__]=m
source=base64.b64decode(os.environ["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
exec(compile(source, "archive_lock.py", "exec"), m.__dict__)
with m._exclusive_archive_sync(pathlib.Path(sys.argv[1])):
    print("LOCKED", flush=True)
    time.sleep(300)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeMappedLockExperiment(unittest.TestCase):
    def test_mapped_view_can_modify_a_locked_byte_without_releasing_the_lock(self) -> None:
        import msvcrt

        with tempfile.TemporaryDirectory(prefix="trackie-map-lock-") as name:
            root = pathlib.Path(name)
            api = native.NativeWin32(experimental=True)
            _volume, kind, fs = api.volume(root.anchor)
            if kind != native._DRIVE_FIXED or fs != "NTFS":
                self.skipTest("requires fixed NTFS")
            lock = root / ".suno-sync.lock"
            lock.write_bytes(b"A")
            env = dict(os.environ)
            if not env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
                path = (
                    pathlib.Path(native.__file__).resolve().parents[1]
                    / "src/suno_dl/archive_lock.py"
                )
                env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(path.read_bytes()).decode()
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD, str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            assert child.stdout is not None
            stdout = child.stdout
            output: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
            try:
                self.assertEqual(output.get(timeout=20).strip(), "LOCKED")
                with lock.open("r+b", buffering=0) as stream:
                    stream.seek(0)
                    with self.assertRaises(OSError):
                        stream.write(b"B")
                    with mmap.mmap(stream.fileno(), 1, access=mmap.ACCESS_WRITE) as mapped:
                        mapped[0:1] = b"C"
                        mapped.flush()
                    stream.seek(0)
                    with self.assertRaises(OSError):
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            finally:
                child.terminate()
                child.wait(timeout=10)
                stdout.close()
            self.assertEqual(lock.read_bytes(), b"C")


if __name__ == "__main__":
    unittest.main(verbosity=2)
