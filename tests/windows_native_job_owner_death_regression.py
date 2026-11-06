"""Disposable native Windows Job Object supervisor termination experiment.

Exercises only an ordinary process tree and an assigned job, not uncooperative
breakaway/WMI children, power loss, trusted provisioning or real archive work.
"""

from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import unittest
from typing import Any

_SUPERVISOR = r"""
import ctypes, subprocess, sys, time
class Basic(ctypes.Structure):
    _fields_ = [("a",ctypes.c_int64),("b",ctypes.c_int64),("flags",ctypes.c_uint32),
        ("min",ctypes.c_size_t),("max",ctypes.c_size_t),("active",ctypes.c_uint32),
        ("affinity",ctypes.c_size_t),("priority",ctypes.c_uint32),("schedule",ctypes.c_uint32)]
class IO(ctypes.Structure):
    _fields_ = [("x"+str(i),ctypes.c_uint64) for i in range(6)]
class Extended(ctypes.Structure):
    _fields_ = [("basic",Basic),("io",IO)]+[("x"+str(i),ctypes.c_size_t) for i in range(4)]
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.CreateJobObjectW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p]
k.CreateJobObjectW.restype=ctypes.c_void_p
k.SetInformationJobObject.argtypes=[ctypes.c_void_p,ctypes.c_int32,ctypes.c_void_p,ctypes.c_uint32]
k.SetInformationJobObject.restype=ctypes.c_int32
k.AssignProcessToJobObject.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
k.AssignProcessToJobObject.restype=ctypes.c_int32
k.GetHandleInformation.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
k.GetHandleInformation.restype=ctypes.c_int32
assert ctypes.sizeof(Extended)==144
job=k.CreateJobObjectW(None,None)
assert job
flags=ctypes.c_uint32()
assert k.GetHandleInformation(job,ctypes.byref(flags)) and not flags.value & 1
info=Extended(); info.basic.flags=0x2000
assert k.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info))
worker_code = "import subprocess,sys,time; line=sys.stdin.readline(); " \
  "sys.exit(2) if line.strip() != 'GO' else None; " \
  "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']," \
  "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); " \
  "print(c.pid,flush=True); time.sleep(300)"
worker=subprocess.Popen([sys.executable,'-c',worker_code],stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,close_fds=True)
assert k.AssignProcessToJobObject(job,int(worker._handle))
worker.stdin.write('GO\n'); worker.stdin.flush()
child=int(worker.stdout.readline().strip())
print('READY',worker.pid,child,flush=True)
time.sleep(300)
"""


@unittest.skipUnless(sys.platform == "win32", "requires Windows")
class SupervisorDeath(unittest.TestCase):
    def test_owner_death_closes_job_and_ends_assigned_tree(self) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("only x64 layout exercised")
        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.TerminateProcess.restype = ctypes.c_int32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int32
        supervisor = subprocess.Popen(
            [sys.executable, "-c", _SUPERVISOR],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        handles: list[Any] = []
        try:
            assert supervisor.stdout is not None
            stdout = supervisor.stdout
            lines: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
            line = lines.get(timeout=20).strip()
            self.assertTrue(line.startswith("READY "), line)
            for pid in line.split()[1:]:
                handle = kernel.OpenProcess(0x00100001, 0, int(pid))
                self.assertTrue(handle)
                handles.append(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
            self.assertEqual(len(handles), 2)
            supervisor.terminate()
            supervisor.wait(timeout=10)
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0)
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=10)
            for handle in handles:
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    kernel.TerminateProcess(handle, 1)
                    kernel.WaitForSingleObject(handle, 10000)
                kernel.CloseHandle(handle)
            if supervisor.stdout is not None:
                supervisor.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
