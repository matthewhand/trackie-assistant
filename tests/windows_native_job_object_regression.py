"""Disposable Windows Job Object process-tree research, not production code.

This tests one ordinary assigned child tree and last-job-handle closure. It does
not establish safe create/assign during supervisor crash, control WMI-created or
breakaway processes, authenticate clients, or establish power-loss durability.
"""

from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import unittest
from typing import Any


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_time", ctypes.c_int64),
        ("per_job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


_WORKER = r"""
import subprocess,sys,time
if sys.stdin.readline().strip() != "GO":
    sys.exit(2)
child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(300)"],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print("CHILD " + str(child.pid),flush=True)
time.sleep(300)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeJobObjectExperiment(unittest.TestCase):
    def _exercise_job(self, terminate_worker_first: bool) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("research layout has only been exercised on x64")
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
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.TerminateProcess.restype = ctypes.c_int32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int32
        info = ExtendedLimits()
        self.assertEqual(ctypes.sizeof(info), 144)
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job)
        worker: Any = None
        child_handle = None
        try:
            self.assertTrue(
                kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", _WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert worker.stdin is not None and worker.stdout is not None
            stdout = worker.stdout
            lines: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
            self.assertTrue(kernel.AssignProcessToJobObject(job, int(worker._handle)))
            worker.stdin.write("GO\n")
            worker.stdin.flush()
            line = lines.get(timeout=20).strip()
            self.assertTrue(line.startswith("CHILD "), line)
            child_handle = kernel.OpenProcess(0x00100001, 0, int(line.split()[1]))
            self.assertTrue(child_handle)
            if terminate_worker_first:
                worker.terminate()
                worker.wait(timeout=10)
                # The child still lives while the supervisor holds the job.
                self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)
            self.assertTrue(kernel.CloseHandle(job))
            job = None
            self.assertEqual(kernel.WaitForSingleObject(int(worker._handle), 10000), 0)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 10000), 0)
        finally:
            if job:
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
                if kernel.WaitForSingleObject(child_handle, 0) == 258:
                    kernel.TerminateProcess(child_handle, 1)
                    kernel.WaitForSingleObject(child_handle, 10000)
                kernel.CloseHandle(child_handle)

    def test_job_close_terminates_an_already_assigned_ordinary_child_tree(self) -> None:
        self._exercise_job(terminate_worker_first=False)

    def test_worker_death_does_not_end_child_until_job_handle_closes(self) -> None:
        self._exercise_job(terminate_worker_first=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
