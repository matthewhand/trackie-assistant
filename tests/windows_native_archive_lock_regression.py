"""Production lock identity checks on a disposable NTFS directory."""

import os
import tempfile
import unittest
from pathlib import Path

from suno_dl.archive_lock import _exclusive_archive_sync


class NativeArchiveLock(unittest.TestCase):
    def test_hardlinked_empty_file_is_never_initialized(self):
        with tempfile.TemporaryDirectory(prefix="trackie-lock-identity-") as temp:
            root = Path(temp) / "archive"
            root.mkdir()
            other = Path(temp) / "other"
            other.write_bytes(b"")
            os.link(other, root / ".suno-sync.lock")
            with self.assertRaises(OSError), _exclusive_archive_sync(root):
                pass
            self.assertEqual(other.read_bytes(), b"")

    def test_ordinary_lock_can_be_reacquired(self):
        with tempfile.TemporaryDirectory(prefix="trackie-lock-valid-") as temp:
            root = Path(temp)
            with _exclusive_archive_sync(root):
                pass
            with _exclusive_archive_sync(root):
                pass
            self.assertEqual((root / ".suno-sync.lock").read_bytes(), b"0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
