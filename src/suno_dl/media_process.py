"""Bounded lifetime for trusted media-tool subprocesses.

Linux/macOS retain subprocess.run behavior. Windows x64 starts the tool suspended,
assigns a non-inheritable kill-on-close job, then resumes its one primary thread.
A normal return requires the retained writer handle to be signalled and the job
to have no active writer. A one-process job limit prevents descendants from
executing; tools requiring helper processes fail. An uncertain fence aborts the
caller, including through broad Exception handlers.

This is process fencing, not durable file publication or a substitute for a
persistent intent. Windows archive writes stay guarded until that integration.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from typing import Any


class ProcessFenceUncertain(BaseException):
    """The caller must stop; surviving writer state is not established."""


class _BasicLimits(ctypes.Structure):
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


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("memory", ctypes.c_size_t * 4),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("faults", ctypes.c_uint32),
        ("total", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
        ("terminated", ctypes.c_uint32),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("owner", ctypes.c_uint32),
        ("base_priority", ctypes.c_int32),
        ("delta_priority", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


def _kernel() -> Any:
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(_ExtendedLimits) != 144:
        raise OSError("Windows media-tool fencing requires the validated x64 ABI")
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows kernel APIs are unavailable")
    kernel = win_dll("kernel32", use_last_error=True)
    ptr = ctypes.c_void_p
    dword = ctypes.c_uint32
    boolean = ctypes.c_int32
    declarations = {
        "CreateJobObjectW": ([ptr, ctypes.c_wchar_p], ptr),
        "SetInformationJobObject": ([ptr, ctypes.c_int32, ptr, dword], boolean),
        "AssignProcessToJobObject": ([ptr, ptr], boolean),
        "TerminateJobObject": ([ptr, dword], boolean),
        "QueryInformationJobObject": ([ptr, ctypes.c_int32, ptr, dword, ptr], boolean),
        "WaitForSingleObject": ([ptr, dword], dword),
        "CloseHandle": ([ptr], boolean),
        "CreateToolhelp32Snapshot": ([dword, dword], ptr),
        "Thread32First": ([ptr, ctypes.POINTER(_ThreadEntry)], boolean),
        "Thread32Next": ([ptr, ctypes.POINTER(_ThreadEntry)], boolean),
        "OpenThread": ([dword, boolean, dword], ptr),
        "ResumeThread": ([ptr], dword),
    }
    for name, (args, result) in declarations.items():
        call = getattr(kernel, name)
        call.argtypes = args
        call.restype = result
    return kernel


def _resume_primary(kernel: Any, process: Any) -> None:
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError("cannot enumerate the suspended writer thread")
    ids = []
    try:
        entry = _ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.size < _ThreadEntry.owner.offset + 4:
                raise OSError("short Windows thread record")
            if entry.owner == process.pid:
                ids.append(entry.thread_id)
            entry.size = ctypes.sizeof(entry)
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        last_error = getattr(ctypes, "get_last_error", None)
        if last_error is None or last_error() != 18:
            raise OSError("incomplete Windows thread enumeration")
    finally:
        kernel.CloseHandle(snapshot)
    if len(ids) != 1:
        raise OSError("suspended media tool has an unexpected thread set")
    thread = kernel.OpenThread(2, 0, ids[0])
    if not thread:
        raise OSError("cannot open the suspended writer thread")
    try:
        if kernel.ResumeThread(thread) != 1:
            raise OSError("writer thread was not singly suspended")
    finally:
        kernel.CloseHandle(thread)


def _fence(kernel: Any, job: Any, process_handle: Any, timeout: float = 10) -> None:
    if not kernel.TerminateJobObject(job, 1):
        raise ProcessFenceUncertain("cannot terminate the owned media-tool job")
    # Job accounting can reach zero before the actual process handle signals.
    # Recovery must wait on the retained handle as well.
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        state = kernel.WaitForSingleObject(process_handle, min(remaining, 25))
        if state not in (0, 258):
            raise ProcessFenceUncertain("cannot observe the owned writer handle")
        info = _Accounting()
        if not kernel.QueryInformationJobObject(
            job, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise ProcessFenceUncertain("cannot observe media-tool job termination")
        if info.total < 1 or info.active > 1:
            raise ProcessFenceUncertain(
                f"unsupported media-tool job accounting: total={info.total}, "
                f"active={info.active}, terminated={info.terminated}"
            )
        if state == 0 and info.active == 0:
            return
        if time.monotonic() >= deadline:
            raise ProcessFenceUncertain("owned media-tool writer did not stop")


def run(
    *popenargs: Any,
    input: Any = None,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    if sys.platform != "win32":
        return subprocess.run(
            *popenargs,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            **kwargs,
        )
    if kwargs.get("shell"):
        raise ValueError("media tools must use a fixed argv without a shell")
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input are mutually exclusive")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output conflicts with explicit streams")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    flags = kwargs.pop("creationflags", 0)
    if flags & 0x01000000:
        raise ValueError("media tools cannot break away from the owned job")
    kwargs["creationflags"] = flags | 0x4 | 0x08000000
    kwargs["close_fds"] = True
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("cannot create owned media-tool job")
    process = None
    assigned = False
    fenced = False
    try:
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x8
        limits.basic.active_limit = 1
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError("cannot configure kill-on-close for the media tool")
        process = subprocess.Popen(*popenargs, **kwargs)
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise OSError("cannot assign suspended media tool to its owned job")
        assigned = True
        _resume_primary(kernel, process)
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _fence(kernel, job, int(process._handle))
            fenced = True
            stdout, stderr = process.communicate()
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        _fence(kernel, job, int(process._handle))
        fenced = True
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        try:
            if process is not None:
                if assigned and not fenced:
                    _fence(kernel, job, int(process._handle))
                elif not assigned:
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                process.wait(timeout=10)
        finally:
            if not kernel.CloseHandle(job):
                raise ProcessFenceUncertain("cannot close media-tool ownership handle")
