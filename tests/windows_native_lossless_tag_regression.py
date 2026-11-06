"""Run production staging writers on real Windows/NTFS; no publication is certified."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from suno_dl.lossless_id3 import validate_artist_update, write_isolated_artist
from suno_dl.lossless_mp4 import validate_mp4_update, write_isolated_mp4

FIXTURES = Path(__file__).parent / "fixtures" / "lossless-native"


class NativeLosslessStaging(unittest.TestCase):
    def test_same_output_bytes_as_linux_on_ntfs_unicode_paths(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        for name, record in manifest.items():
            with (
                self.subTest(format=name),
                tempfile.TemporaryDirectory(prefix="trackie-native-tags-") as temp,
            ):
                target = Path(temp) / ("producer-\u03bb" + Path(name).suffix)
                shutil.copyfile(FIXTURES / name, target)
                before = target.read_bytes()
                if target.suffix == ".m4a":
                    validate_mp4_update(target, {b"\xa9ART": "Verified \u03bb producer"})
                    self.assertEqual(target.read_bytes(), before)
                    write_isolated_mp4(target, {b"\xa9ART": "Verified \u03bb producer"})
                else:
                    validate_artist_update(target, target.suffix, "Verified \u03bb producer")
                    self.assertEqual(target.read_bytes(), before)
                    write_isolated_artist(target, target.suffix, "Verified \u03bb producer")
                self.assertEqual(
                    hashlib.sha256(target.read_bytes()).hexdigest(), record["output_sha256"]
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
