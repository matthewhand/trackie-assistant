"""Research only: fence one ordinary Windows process tree under a held lock.

The in-memory controller must survive the failed worker. Recovery after a
controller exit, reboot or power loss is unsupported. This excludes escaped,
WMI-created and unrelated writers, shared-handle manipulation, untrusted lock
paths, journal rollback and any production integration. Job termination does
not itself prove filesystem durability or provide atomic group visibility.
"""

from __future__ import annotations

import base64
import ctypes
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from typing import Any


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("memory", ctypes.c_size_t * 4),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("faults", ctypes.c_uint32),
        ("total", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
        ("terminated", ctypes.c_uint32),
    ]


def fence(kernel: Any, job: Any, timeout: float = 10.0, *, writer_handles=()) -> None:
    """Request termination and observe no remaining job processes, or stop."""
    if not kernel.TerminateJobObject(job, 1):
        raise RuntimeError("could not terminate job")
    deadline = time.monotonic() + timeout
    while True:
        info = Accounting()
        if not kernel.QueryInformationJobObject(
            job, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise RuntimeError("could not check job termination")
        stopped = all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in writer_handles)
        if info.active == 0 and stopped:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("job still contains processes")
        time.sleep(0.025)


_WORKER = r"""
import subprocess,sys,time
if sys.stdin.readline().strip() != 'GO': sys.exit(2)
child = "import pathlib,sys,time\n" + \
 "signal,output=map(pathlib.Path,sys.argv[1:])\n" + \
 "for _ in range(400):\n" + \
 " if signal.exists(): output.write_bytes(b'late'); break\n" + \
 " time.sleep(0.025)\n"
p=subprocess.Popen([sys.executable,'-c',child,sys.argv[1],sys.argv[2]],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
print('CHILD '+str(p.pid),flush=True)
time.sleep(300)
"""


class FenceFailures(unittest.TestCase):
    def test_fence_failure_stops_recovery(self) -> None:
        class Broken:
            def TerminateJobObject(self, *_args: Any) -> int:
                return 0

        recovery_called = False
        with self.assertRaises(RuntimeError):
            fence(Broken(), 123)
            recovery_called = True
        self.assertFalse(recovery_called)

    def test_unreadable_job_state_stops_recovery(self) -> None:
        class Broken:
            def TerminateJobObject(self, *_args: Any) -> int:
                return 1

            def QueryInformationJobObject(self, *_args: Any) -> int:
                return 0

        with self.assertRaises(RuntimeError):
            fence(Broken(), 123)

    def test_remaining_process_stops_recovery(self) -> None:
        class StillActive:
            def TerminateJobObject(self, *_args: Any) -> int:
                return 1

            def QueryInformationJobObject(
                self, _job: Any, _kind: Any, buf: Any, _size: Any, _length: Any
            ) -> int:
                ctypes.cast(buf, ctypes.POINTER(Accounting)).contents.active = 1
                return 1

        with self.assertRaises(RuntimeError):
            fence(StillActive(), 123, timeout=0)


@unittest.skipUnless(sys.platform == "win32", "requires Windows")
class LiveSupervisorFence(unittest.TestCase):
    def test_fences_surviving_child_before_recovery_under_held_lock(self) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("only x64 research layout exercised")
        lock = types.ModuleType("trackie_live_session_lock")
        sys.modules[lock.__name__] = lock
        encoded = os.environ.get("TRACKIE_ARCHIVE_LOCK_MODEL")
        source = (
            base64.b64decode(encoded).decode()
            if encoded
            else (
                pathlib.Path(__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            ).read_text()
        )
        exec(compile(source, "archive_lock.py", "exec"), lock.__dict__)  # noqa: S102
        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel.SetInformationJobObject.restype = ctypes.c_int32
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int32
        kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.TerminateJobObject.restype = ctypes.c_int32
        kernel.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel.QueryInformationJobObject.restype = ctypes.c_int32
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int32
        self.assertEqual(ctypes.sizeof(ExtendedLimits), 144)
        self.assertEqual(ctypes.sizeof(Accounting), 48)
        with tempfile.TemporaryDirectory(prefix="trackie-fence-") as tmp:
            root = pathlib.Path(tmp)
            with lock._exclusive_archive_sync(root):
                job = kernel.CreateJobObjectW(None, None)
                self.assertTrue(job)
                worker: Any = None
                child_handle = None
                try:
                    limits = ExtendedLimits()
                    limits.basic.flags = 0x2000
                    self.assertTrue(
                        kernel.SetInformationJobObject(
                            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                        )
                    )
                    worker = subprocess.Popen(
                        [sys.executable, "-c", _WORKER, str(root / "signal"), str(root / "late")],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    assert worker.stdout is not None and worker.stdin is not None
                    lines: queue.Queue[str] = queue.Queue()
                    threading.Thread(
                        target=lambda: lines.put(worker.stdout.readline()), daemon=True
                    ).start()
                    self.assertTrue(kernel.AssignProcessToJobObject(job, int(worker._handle)))
                    worker.stdin.write("GO\n")
                    worker.stdin.flush()
                    line = lines.get(timeout=20).strip()
                    self.assertTrue(line.startswith("CHILD "), line)
                    child_handle = kernel.OpenProcess(0x00100000, 0, int(line.split()[1]))
                    self.assertTrue(child_handle)
                    worker.terminate()
                    worker.wait(timeout=10)
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)
                    fence(kernel, job, writer_handles=(child_handle,))
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 0)
                    (root / "signal").write_bytes(b"go")
                    (root / "recovery").write_bytes(b"reviewed fixture")
                    self.assertFalse((root / "late").exists())
                finally:
                    kernel.CloseHandle(job)
                    if worker is not None:
                        if worker.poll() is None:
                            worker.terminate()
                            worker.wait(timeout=10)
                        if worker.stdin is not None:
                            worker.stdin.close()
                        if worker.stdout is not None:
                            worker.stdout.close()
                    if child_handle:
                        kernel.WaitForSingleObject(child_handle, 10000)
                        kernel.CloseHandle(child_handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
