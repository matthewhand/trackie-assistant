"""Real FFmpeg/ffprobe process fencing on disposable Windows media only.

Supply direct executables, not launchers, through TRACKIE_TEST_FFMPEG and
TRACKIE_TEST_FFPROBE. No tool is downloaded and no archive is opened.
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from suno_dl import media_process as media


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class NativeFFmpegFence(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = os.environ.get("TRACKIE_TEST_FFMPEG")
        self.ffprobe = os.environ.get("TRACKIE_TEST_FFPROBE")
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("supply direct installed FFmpeg and ffprobe paths")
        self.assertTrue(Path(self.ffmpeg).is_file())
        self.assertTrue(Path(self.ffprobe).is_file())
        self.fixture = Path(__file__).parent / "fixtures" / "lossless-native"

    def test_real_tools_decode_and_probe_each_supported_format(self):
        for suffix in ("m4a", "mp3", "wav"):
            with self.subTest(format=suffix):
                path = self.fixture / ("sample." + suffix)
                decoded = media.run(
                    [
                        self.ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-err_detect",
                        "explode",
                        "-i",
                        str(path),
                        "-map",
                        "0:a:0",
                        "-f",
                        "hash",
                        "-hash",
                        "sha256",
                        "-",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertRegex(decoded.stdout.strip(), r"^SHA256=[a-f0-9]{64}$")
                probe = media.run(
                    [
                        self.ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type",
                        "-of",
                        "json",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(json.loads(probe.stdout)["streams"], [{"codec_type": "audio"}])

    def test_killing_owner_stops_real_ffmpeg_before_recovery(self):
        with tempfile.TemporaryDirectory(prefix="trackie-ffmpeg-owner-") as temp:
            root = Path(temp)
            ready, output = root / "pid", root / "private-stage.wav"
            source = Path(__file__).resolve().parents[1] / "src"
            owner = """
import sys,pathlib
sys.path.insert(0,sys.argv[1])
from suno_dl import media_process as media
original=media._resume_primary
def resumed(kernel,process):
 original(kernel,process)
 pathlib.Path(sys.argv[2]).write_text(str(process.pid))
media._resume_primary=resumed
media.run([sys.argv[3],'-nostdin','-v','error','-re','-stream_loop','-1',
 '-i',sys.argv[4],'-c:a','copy','-flush_packets','1','-f','wav',sys.argv[5]],timeout=120)
"""
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    owner,
                    str(source),
                    str(ready),
                    self.ffmpeg,
                    str(self.fixture / "sample.wav"),
                    str(output),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            kernel = media._kernel()
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = None
            try:
                deadline = time.monotonic() + 15
                while not (
                    ready.exists()
                    and ready.stat().st_size
                    and output.exists()
                    and output.stat().st_size
                ):
                    if proc.poll() is not None:
                        self.fail(
                            "FFmpeg owner exited: " + proc.stderr.read().decode(errors="replace")
                        )
                    self.assertLess(time.monotonic(), deadline, "FFmpeg did not start")
                    time.sleep(0.02)
                handle = kernel.OpenProcess(0x00100000, 0, int(ready.read_text()))
                self.assertTrue(handle)
                proc.kill()
                proc.wait(timeout=10)
                self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0)
                size = output.stat().st_size
                time.sleep(0.2)
                self.assertEqual(output.stat().st_size, size)
                # The output can now be opened exclusively for an explicit recovery.
                with output.open("r+b") as stream:
                    stream.seek(0, 2)
                    self.assertEqual(stream.tell(), size)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                if handle:
                    kernel.CloseHandle(handle)
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
