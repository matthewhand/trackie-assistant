"""Disposable NTFS tests for the unintegrated two-file research model.

Process termination is not power loss; tests do not authenticate external
binding, prove atomic group visibility or support existing real libraries.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import windows_bound_multifile_model as multi
import windows_bound_publish_model as single
import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as journal

_CHILD = r"""
import base64, os, pathlib, sys, time, types
for name, key in (("windows_filesystem_adapter_model", "TRACKIE_NATIVE_MODEL"),
                  ("windows_recovery_model", "TRACKIE_RECOVERY_MODEL"),
                  ("windows_persistent_journal_experiment", "TRACKIE_JOURNAL_MODEL"),
                  ("windows_bound_publish_model", "TRACKIE_PUBLISH_MODEL"),
                  ("windows_bound_multifile_model", "TRACKIE_MULTI_MODEL")):
    module = types.ModuleType(name); sys.modules[name] = module
    source = base64.b64decode(os.environ[key]).decode("utf-8")
    exec(compile(source, name + ".py", "exec"), module.__dict__)
import windows_bound_multifile_model as m
phase, root, volume, high, low, identifier = sys.argv[1:]
binding = m.ExternalBinding(int(volume), int(high), int(low), identifier)
def pause(state):
    if state == phase:
        print("READY " + phase, flush=True); time.sleep(300)
with m.MultiFilePublisher(pathlib.Path(root), binding) as session:
    session.begin(b"synthetic media sentinel", hook=pause)
"""


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class NativeMultiFileExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="trackie-w-multifile-")
        self.addCleanup(self.temp.cleanup)
        self.api = native.NativeWin32(experimental=True)
        handle = self.api.open(self.temp.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(native._canonical_name(self.api.final_path(handle)))
        finally:
            self.api.close(handle)
        _volume, kind, fs = self.api.volume(self.root.anchor)
        if kind != native._DRIVE_FIXED or fs != "NTFS":
            self.skipTest("requires fixed NTFS")
        self.env = dict(os.environ)
        modules = (
            (native, "TRACKIE_NATIVE_MODEL"),
            (journal, "TRACKIE_JOURNAL_MODEL"),
            (single, "TRACKIE_PUBLISH_MODEL"),
            (multi, "TRACKIE_MULTI_MODEL"),
        )
        for module, key in modules:
            if not self.env.get(key):
                self.env[key] = base64.b64encode(
                    pathlib.Path(module.__file__).read_bytes()
                ).decode()
        if not self.env.get("TRACKIE_RECOVERY_MODEL"):
            path = pathlib.Path(multi.__file__).with_name("windows_recovery_model.py")
            self.env["TRACKIE_RECOVERY_MODEL"] = base64.b64encode(path.read_bytes()).decode()

    def _fixture(self, name: str) -> tuple[pathlib.Path, multi.ExternalBinding]:
        root = self.root / name
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        identifier = uuid.uuid4().hex
        path = root / "journal.bin"
        with path.open("xb", buffering=0) as stream:
            stream.write(journal.MAGIC + b"ID=" + identifier.encode() + b"\n")
            stream.write(journal._frame({"phase": "multi-format", "version": 1}))
            os.fsync(stream.fileno())
        handle = self.api.open(str(path), directory=False, writable=False)
        try:
            info = self.api.info(handle)
        finally:
            self.api.close(handle)
        return root, multi.ExternalBinding(
            info.volume_serial, info.index_high, info.index_low, identifier
        )

    def _stop_at(self, root: pathlib.Path, binding: multi.ExternalBinding, phase: str) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                phase,
                str(root),
                str(binding.volume_serial),
                str(binding.index_high),
                str(binding.index_low),
                binding.journal_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        output: queue.Queue[str] = queue.Queue()
        assert child.stdout is not None
        stdout = child.stdout
        threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(output.get(timeout=20).strip(), "READY " + phase)
        finally:
            child.terminate()
            child.wait(timeout=10)
            stdout.close()

    def _check_final(self, root: pathlib.Path) -> None:
        self.assertEqual((root / "media.wav").read_bytes(), b"synthetic media sentinel")
        data = json.loads((root / "media.json").read_bytes())
        self.assertEqual(data["saved_files"], ["media.wav"])
        self.assertEqual(str(uuid.UUID(data["id"])), data["id"])

    def test_process_kills_at_multifile_boundaries(self) -> None:
        blocked = {
            "media_stage_created",
            "media_stage_written",
            "media_stage_flushed",
            "sidecar_stage_created",
            "sidecar_stage_written",
            "sidecar_stage_flushed",
            "intent_partial",
            "complete_partial",
        }
        phases = (
            *sorted(blocked),
            "intent_written",
            "intent_flushed",
            "media_published",
            "sidecar_published",
            "complete_written",
            "complete_flushed",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                root, binding = self._fixture(phase)
                self._stop_at(root, binding, phase)
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                if phase == "media_published":
                    self.assertTrue((root / "media.wav").exists())
                    self.assertFalse((root / "media.json").exists())
                with multi.MultiFilePublisher(root, binding) as session:
                    if phase in blocked:
                        with self.assertRaises(multi.BindingError):
                            session.recover()
                    else:
                        self.assertEqual(session.recover(), "complete")
                        self.assertEqual(session.recover(), "complete")
                        self._check_final(root)
                if phase in blocked:
                    self.assertEqual(
                        {path.name: path.read_bytes() for path in root.iterdir()}, before
                    )

    def test_unexplained_member_raises_binding_error_without_changes(self) -> None:
        root, binding = self._fixture("unexplained")
        extra = root / "other.bin"
        extra.write_bytes(b"existing sentinel")
        with multi.MultiFilePublisher(root, binding) as session:
            with self.assertRaises(multi.BindingError):
                session.classify()
            with self.assertRaises(multi.BindingError):
                session.recover()
        self.assertEqual(extra.read_bytes(), b"existing sentinel")
        self.assertFalse((root / "media.wav").exists())

    def test_preflight_checks_both_members_before_first_publication(self) -> None:
        root, binding = self._fixture("preflight")
        self._stop_at(root, binding, "intent_flushed")
        sidecar = root / ".stage-sidecar"
        payload = bytearray(sidecar.read_bytes())
        payload[0] ^= 1
        sidecar.write_bytes(payload)
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        with (
            multi.MultiFilePublisher(root, binding) as session,
            self.assertRaises(multi.BindingError),
        ):
            session.recover()
        self.assertFalse((root / "media.wav").exists())
        self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_second_member_flush_error_stops_before_first_publication(self) -> None:
        import msvcrt

        root, binding = self._fixture("sidecar-flush")
        self._stop_at(root, binding, "intent_flushed")
        with multi.MultiFilePublisher(root, binding) as session:
            real = multi.os.fsync

            def fail_sidecar(fd):
                path = self.api.final_path(msvcrt.get_osfhandle(fd))
                if path.lower().endswith("\\.stage-sidecar"):
                    raise OSError("injected sidecar flush error")
                return real(fd)

            with (
                patch.object(multi.os, "fsync", side_effect=fail_sidecar),
                self.assertRaises(multi.BindingError),
            ):
                session.recover()
            self.assertFalse((root / "media.wav").exists())
            self.assertTrue((root / ".stage-media").exists())
            self.assertTrue((root / ".stage-sidecar").exists())
            self.assertFalse((root / "media.json").exists())
            self.assertEqual(session.classify(), "pending-stage-stage")
            self.assertEqual(session.recover(), "complete")
            self._check_final(root)

    def test_published_media_flush_failure_stops_sidecar_publication(self) -> None:
        import msvcrt

        root, binding = self._fixture("media-target-flush")
        self._stop_at(root, binding, "media_published")
        with multi.MultiFilePublisher(root, binding) as session:
            real = multi.os.fsync

            def fail_media_target(fd):
                path = self.api.final_path(msvcrt.get_osfhandle(fd))
                if path.lower().endswith("\\media.wav"):
                    raise OSError("injected published media flush error")
                return real(fd)

            with (
                patch.object(multi.os, "fsync", side_effect=fail_media_target),
                self.assertRaises(multi.BindingError),
            ):
                session.recover()
            self.assertTrue((root / "media.wav").exists())
            self.assertTrue((root / ".stage-sidecar").exists())
            self.assertFalse((root / "media.json").exists())
            self.assertEqual(session.classify(), "pending-target-stage")
            self.assertEqual(session.recover(), "complete")
            self._check_final(root)

    def test_sidecar_first_state_is_verified_then_completed(self) -> None:
        root, binding = self._fixture("sidecar-first")
        self._stop_at(root, binding, "intent_flushed")
        stage_sidecar, target_sidecar = root / ".stage-sidecar", root / "media.json"
        original = stage_sidecar.read_bytes()
        with multi.MultiFilePublisher(root, binding) as session:
            # Construct a verified out-of-order state in disposable storage;
            # this does not prove how NTFS behaves after a real power loss.
            adapter = native.Adapter(self.api, archive_lock_held=True)
            adapter.publish(
                str(stage_sidecar),
                str(target_sidecar),
                archive_lock_held=True,
                staged_file_owned=True,
            )
            self.assertFalse((root / "media.wav").exists())
            self.assertEqual(target_sidecar.read_bytes(), original)
            self.assertEqual(session.classify(), "pending-stage-target")
            self.assertEqual(session.recover(), "complete")
            self.assertEqual(target_sidecar.read_bytes(), original)
            self._check_final(root)

    def test_persistent_journal_flush_failure_stops_before_first_publication(self) -> None:
        root, binding = self._fixture("journal-flush")
        self._stop_at(root, binding, "intent_flushed")
        with multi.MultiFilePublisher(root, binding) as session:
            with (
                patch.object(multi.os, "fsync", side_effect=OSError("injected")),
                self.assertRaises(multi.BindingError),
            ):
                session.recover()
            self.assertFalse((root / "media.wav").exists())
            self.assertFalse((root / "media.json").exists())
            self.assertEqual(session.recover(), "complete")
            self._check_final(root)

    def test_completion_fsync_error_is_observation_not_acknowledgement(self) -> None:
        root, binding = self._fixture("completion-flush")
        self._stop_at(root, binding, "intent_flushed")
        with multi.MultiFilePublisher(root, binding) as session:
            original = session._append

            def fail_complete(record, phase, hook):
                if phase == "complete":
                    with patch.object(multi.os, "fsync", side_effect=OSError("injected")):
                        return original(record, phase, hook)
                return original(record, phase, hook)

            with (
                patch.object(session, "_append", side_effect=fail_complete),
                self.assertRaises(multi.BindingError),
            ):
                session.recover()
            self._check_final(root)
            self.assertEqual(session.classify(), "completion-frame-visible")
            with (
                patch.object(multi.os, "fsync", side_effect=OSError("still failing")),
                self.assertRaises(multi.BindingError),
            ):
                session.recover()
            self.assertEqual(session.recover(), "complete")

    def test_reported_error_after_each_real_rename_reconciles_by_identity(self) -> None:
        for role in ("media", "sidecar"):
            with self.subTest(role=role):
                root, binding = self._fixture("rename-error-" + role)
                self._stop_at(root, binding, "intent_flushed")
                with multi.MultiFilePublisher(root, binding) as session:
                    original = native.Adapter.publish

                    def fail_after(
                        adapter, source, destination, role=role, original=original, **kwargs
                    ):
                        original(adapter, source, destination, **kwargs)
                        if pathlib.Path(source).name == ".stage-" + role:
                            raise native.AdapterError("injected ambiguous rename")

                    with (
                        patch.object(native.Adapter, "publish", new=fail_after),
                        self.assertRaises(native.AdapterError),
                    ):
                        session.recover()
                    self.assertEqual(session.recover(), "complete")
                    self._check_final(root)

    def test_format_record_prevents_single_file_protocol_mixup(self) -> None:
        root, binding = self._fixture("format")
        with (
            single.BoundPublisher(root, binding) as session,
            self.assertRaises(single.BindingError),
        ):
            session.begin(b"must not publish")
        self.assertEqual({p.name for p in root.iterdir()}, {"journal.bin", ".suno-sync.lock"})

    def test_externally_supplied_binding_must_be_trusted(self) -> None:
        root, binding = self._fixture("forged-binding")
        path, original = root / "journal.bin", self.root / "original-journal"
        path.rename(original)
        path.write_bytes(original.read_bytes())
        with self.assertRaises(multi.BindingError), multi.MultiFilePublisher(root, binding):
            pass
        h = self.api.open(str(path), directory=False, writable=False)
        try:
            info = self.api.info(h)
        finally:
            self.api.close(h)
        attacker_supplied = multi.ExternalBinding(
            info.volume_serial, info.index_high, info.index_low, binding.journal_id
        )
        # Supplying a freshly captured impostor ID bypasses the expectation:
        # the model cannot authenticate its caller's purported external binding.
        with multi.MultiFilePublisher(root, attacker_supplied) as session:
            self.assertEqual(session.classify(), "empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
