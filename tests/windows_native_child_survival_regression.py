"""Disposable Windows process-tree and common-lock limitation test.

A deliberately noncooperating descendant demonstrates that parent exit and
lock reacquisition do not prove all writers have stopped. This is not a
production recovery implementation or a power-loss test.
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
import types
import unittest

import windows_filesystem_adapter_model as native


class _Basic(ctypes.Structure):
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


class _IoCounters(ctypes.Structure):
    _fields_ = [(f"counter_{i}", ctypes.c_uint64) for i in range(6)]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("basic", _Basic),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


_WORKER = r"""
import base64,os,pathlib,subprocess,sys,time,types
if sys.stdin.readline().strip() != "GO": sys.exit(2)
m=types.ModuleType("trackie_lock");sys.modules[m.__name__]=m
exec(compile(base64.b64decode(os.environ["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode(),"lock.py","exec"),m.__dict__)
root=pathlib.Path(sys.argv[1])
child_code = "import pathlib,sys,time\n" + \
    "signal,output=map(pathlib.Path,sys.argv[1:])\n" + \
    "for i in range(400):\n" + \
    " if signal.exists(): output.write_bytes(b'descendant-wrote'); break\n" + \
    " time.sleep(0.025)\n"
with m._exclusive_archive_sync(root):
    p=subprocess.Popen([sys.executable,"-c",child_code,str(root/"signal"),str(root/"output")],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print("CHILD "+str(p.pid),flush=True);time.sleep(300)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeChildSurvivalExperiment(unittest.TestCase):
    def test_worker_exit_does_not_quiesce_its_uncooperative_descendant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trackie-child-survive-") as name:
            root = pathlib.Path(name)
            api = native.NativeWin32(experimental=True)
            _volume, kind, fs = api.volume(root.anchor)
            if kind != native._DRIVE_FIXED or fs != "NTFS":
                self.skipTest("requires fixed NTFS")
            env = dict(os.environ)
            if not env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
                path = (
                    pathlib.Path(native.__file__).resolve().parents[1]
                    / "src/suno_dl/archive_lock.py"
                )
                env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(path.read_bytes()).decode()
            lock = types.ModuleType("trackie_test_existing_lock")
            sys.modules[lock.__name__] = lock
            source = base64.b64decode(env["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
            exec(compile(source, "archive_lock.py", "exec"), lock.__dict__)  # noqa: S102
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.TerminateProcess.restype = ctypes.c_int32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int32
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
            self.assertEqual(ctypes.sizeof(_Extended), 144)
            job = kernel.CreateJobObjectW(None, None)
            self.assertTrue(job)
            limits = _Extended()
            limits.basic.flags = 0x2000  # kill assigned processes when last job handle closes
            if not kernel.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                kernel.CloseHandle(job)
                self.fail("could not configure cleanup job")
            try:
                worker = subprocess.Popen(
                    [sys.executable, "-c", _WORKER, str(root)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
            except BaseException:
                kernel.CloseHandle(job)
                raise
            assert worker.stdout is not None
            stdout = worker.stdout
            lines: queue.Queue[str] = queue.Queue()
            child_handle = None
            try:
                threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
                self.assertTrue(kernel.AssignProcessToJobObject(job, int(worker._handle)))  # type: ignore[attr-defined]
                assert worker.stdin is not None
                worker.stdin.write("GO\n")
                worker.stdin.flush()
                line = lines.get(timeout=20).strip()
                self.assertTrue(line.startswith("CHILD "), line)
                pid = int(line.split()[1])
                # Hold the exact process object, avoiding PID reuse during cleanup.
                child_handle = kernel.OpenProcess(0x00100001, 0, pid)
                self.assertTrue(child_handle)
                worker.terminate()
                worker.wait(timeout=10)
                self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)
                with lock._exclusive_archive_sync(root):
                    (root / "signal").write_bytes(b"go")
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 12000), 0)
                    self.assertEqual((root / "output").read_bytes(), b"descendant-wrote")
            finally:
                # This kills all assigned test descendants even if no child handle was obtained.
                kernel.CloseHandle(job)
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=10)
                if child_handle:
                    if kernel.WaitForSingleObject(child_handle, 0) == 258:
                        kernel.TerminateProcess(child_handle, 1)
                        kernel.WaitForSingleObject(child_handle, 10000)
                    kernel.CloseHandle(child_handle)
                stdout.close()
                if worker.stdin is not None:
                    worker.stdin.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
