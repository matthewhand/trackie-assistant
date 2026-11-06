"""Native tests of the production media-tool supervisor; no library writes."""

import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from suno_dl import media_process as media


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class NativeMediaProcess(unittest.TestCase):
    def test_capture_and_exit_status(self):
        r = media.run(
            [sys.executable, "-c", "print('hello')"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(r.stdout.strip(), "hello")
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            media.run(
                [sys.executable, "-c", "print('failed');raise SystemExit(7)"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        self.assertEqual(caught.exception.returncode, 7)

    def test_failure_before_resume_never_executes_writer(self):
        with tempfile.TemporaryDirectory(prefix="trackie-suspended-") as temp:
            output = Path(temp) / "unexpected"
            with (
                patch.object(media, "_resume_primary", side_effect=OSError("injected")),
                self.assertRaises(OSError),
            ):
                media.run(
                    [
                        sys.executable,
                        "-c",
                        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')",
                        str(output),
                    ],
                    timeout=10,
                )
            self.assertFalse(output.exists())

    def test_timeout_stops_the_actual_writer(self):
        with tempfile.TemporaryDirectory(prefix="trackie-timeout-") as temp:
            output = Path(temp) / "late"
            script = (
                "import pathlib,sys,time;time.sleep(.25);"
                "pathlib.Path(sys.argv[1]).write_text('late')"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                media.run(
                    [sys.executable, "-c", script, str(output)], capture_output=True, timeout=0.05
                )
            time.sleep(0.35)
            self.assertFalse(output.exists())

    def test_owner_death_kills_writer_before_any_late_write(self):
        with tempfile.TemporaryDirectory(prefix="trackie-owner-death-") as temp:
            root = Path(temp)
            ready = root / "ready"
            signal = root / "go"
            late = root / "late"
            writer = (
                "import pathlib,sys,time,os\n"
                "ready,signal,late=map(pathlib.Path,sys.argv[1:])\n"
                "ready.write_text(str(os.getpid()))\n"
                "for _ in range(400):\n"
                " if signal.exists(): late.write_text('late');break\n"
                " time.sleep(.025)\n"
            )
            owner = (
                "import sys\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "from suno_dl.media_process import run\n"
                "run([sys.executable,'-c',sys.argv[2],*sys.argv[3:]],timeout=30)\n"
            )
            source = Path(__file__).resolve().parents[1] / "src"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    owner,
                    str(source),
                    writer,
                    str(ready),
                    str(signal),
                    str(late),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            handle = None
            kernel = media._kernel()
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            try:
                deadline = time.monotonic() + 10
                while (
                    not ready.exists() or not ready.stat().st_size
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                handle = kernel.OpenProcess(0x00100000, 0, int(ready.read_text()))
                self.assertTrue(handle)
                proc.kill()
                proc.wait(timeout=10)
                self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0)
                signal.write_text("go")
                time.sleep(0.1)
                self.assertFalse(late.exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                if handle:
                    kernel.CloseHandle(handle)

    def test_descendant_cannot_run_or_write_a_file(self):
        with tempfile.TemporaryDirectory(prefix="trackie-child-denied-") as temp:
            target = Path(temp) / "unexpected"
            child = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')"
            parent = (
                "import subprocess,sys;subprocess.run([sys.executable,'-c',sys.argv[1],"
                "sys.argv[2]],check=True)"
            )
            with self.assertRaises(subprocess.CalledProcessError):
                media.run(
                    [sys.executable, "-c", parent, child, str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            self.assertFalse(target.exists())


class AccountingRace(unittest.TestCase):
    def test_zero_accounting_waits_for_writer_handle_signal(self):
        class Kernel:
            calls = 0

            def TerminateJobObject(self, *args):
                return 1

            def WaitForSingleObject(self, *args):
                self.calls += 1
                return 0 if self.calls == 3 else 258

            def QueryInformationJobObject(self, job, kind, pointer, size, length):
                info = ctypes.cast(pointer, ctypes.POINTER(media._Accounting)).contents
                info.total = 1
                info.active = 0
                return 1

        kernel = Kernel()
        media._fence(kernel, 123, 456)
        self.assertEqual(kernel.calls, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
