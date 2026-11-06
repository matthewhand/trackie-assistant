"""Manually run disposable Win32 backend checks on a local NTFS volume.

Run ``python tests/windows_native_regression.py`` on Windows. This exercises
only the experimental NativeWin32 backend and does not establish full adapter,
concurrent-writer, crash, or power-loss safety. It needs no third-party modules.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

import windows_filesystem_adapter_model as model


@unittest.skipUnless(sys.platform == "win32", "requires a real Windows host")
class NativeRenameRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="trackie-ntfs-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.api = model.NativeWin32(experimental=True)
        _volume, drive_type, filesystem = self.api.volume(self.root.anchor)
        if drive_type != model._DRIVE_FIXED or filesystem != "NTFS":
            self.skipTest("requires a fixed NTFS temporary volume")

    def test_no_replace_rename_exact_unicode_names(self) -> None:
        for n in range(160):
            with self.subTest(length=n):
                source = self.root / f"source{n:03}.bin"
                target = self.root / (f"target{n:03}" + "x" * (n % 48) + "\u03bb.bin")
                source.write_bytes(b"disposable sentinel")
                before = {path.name for path in self.root.iterdir()}
                handle = self.api.open(str(source), directory=False, writable=True)
                try:
                    self.api.flush(handle)
                    self.api.rename_no_replace(handle, str(target))
                finally:
                    self.api.close(handle)
                created = {path.name for path in self.root.iterdir()} - (before - {source.name})
                self.assertEqual(created, {target.name})
                self.assertFalse(source.exists())
                self.assertEqual(target.read_bytes(), b"disposable sentinel")

    def test_full_adapter_publish_and_collision(self) -> None:
        handle = self.api.open(str(self.root), directory=True, writable=False)
        try:
            canonical = model._canonical_name(self.api.final_path(handle))
        finally:
            self.api.close(handle)
        root = pathlib.Path(canonical)
        adapter = model.Adapter(self.api, archive_lock_held=True)
        source, target = root / "stage-λ.bin", root / "published-λ.bin"
        with source.open("xb") as stream:
            stream.write(b"owned staging sentinel")
        adapter.publish(str(source), str(target), archive_lock_held=True, staged_file_owned=True)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"owned staging sentinel")
        with source.open("xb") as stream:
            stream.write(b"collision source")
        with self.assertRaises(model.AdapterError):
            adapter.publish(
                str(source), str(target), archive_lock_held=True, staged_file_owned=True
            )
        self.assertEqual(source.read_bytes(), b"collision source")
        self.assertEqual(target.read_bytes(), b"owned staging sentinel")

    def test_full_adapter_rejects_hardlink(self) -> None:
        handle = self.api.open(str(self.root), directory=True, writable=False)
        try:
            root = pathlib.Path(model._canonical_name(self.api.final_path(handle)))
        finally:
            self.api.close(handle)
        source, alias, target = root / "stage.bin", root / "alias.bin", root / "target.bin"
        source.write_bytes(b"source sentinel")
        os.link(source, alias)
        adapter = model.Adapter(self.api, archive_lock_held=True)
        with self.assertRaises(model.AdapterError):
            adapter.publish(
                str(source), str(target), archive_lock_held=True, staged_file_owned=True
            )
        self.assertEqual(source.read_bytes(), b"source sentinel")
        self.assertFalse(target.exists())

    def test_full_adapter_rejects_reparse_directory(self) -> None:
        handle = self.api.open(str(self.root), directory=True, writable=False)
        try:
            root = pathlib.Path(model._canonical_name(self.api.final_path(handle)))
        finally:
            self.api.close(handle)
        real, linked, target = root / "real", root / "linked", root / "target.bin"
        real.mkdir()
        (real / "stage.bin").write_bytes(b"source sentinel")
        try:
            os.symlink(real, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"creating a disposable directory symlink is unavailable: {exc}")
        adapter = model.Adapter(self.api, archive_lock_held=True)
        with self.assertRaises(model.AdapterError):
            adapter.publish(
                str(linked / "stage.bin"),
                str(target),
                archive_lock_held=True,
                staged_file_owned=True,
            )
        self.assertEqual((real / "stage.bin").read_bytes(), b"source sentinel")
        self.assertFalse(target.exists())

    def test_existing_target_is_untouched(self) -> None:
        source, target = self.root / "source.bin", self.root / "target.bin"
        source.write_bytes(b"source sentinel")
        target.write_bytes(b"target sentinel")
        handle = self.api.open(str(source), directory=False, writable=True)
        try:
            with self.assertRaises(model.AdapterError):
                self.api.rename_no_replace(handle, str(target))
        finally:
            self.api.close(handle)
        self.assertEqual(source.read_bytes(), b"source sentinel")
        self.assertEqual(target.read_bytes(), b"target sentinel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
