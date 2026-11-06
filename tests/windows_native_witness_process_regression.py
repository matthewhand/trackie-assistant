"""Disposable Windows process-kill tests for a deliberately fake witness.

Journal and mock witness share one NTFS volume. The mock is unauthenticated,
non-monotonic and can be rolled back. These tests do not validate an independent
witness service, transaction protocol, power loss or production integration.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import types
import unittest

import windows_external_witness_model as witness
import windows_filesystem_adapter_model as native
import windows_multifile_grammar_regression as grammar
import windows_persistent_journal_experiment as journal

_ID = "a" * 32
_CHILD = r"""
import base64, json, os, pathlib, sys, time, types
lock=types.ModuleType("trackie_lock");sys.modules[lock.__name__]=lock
source=base64.b64decode(os.environ["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
exec(compile(source,"lock.py","exec"),lock.__dict__)
phase, root = sys.argv[1], pathlib.Path(sys.argv[2])
data=json.loads(os.environ["TRACKIE_TEST_WITNESS_DATA"])
suffix=base64.b64decode(data["suffix"])
next_witness=data["next_witness"].encode("ascii")
def pause(value):
    if phase == value: print("READY " + value,flush=True);time.sleep(300)
with lock._exclusive_archive_sync(root):
    with (root / "journal.bin").open("ab", buffering=0) as stream:
        if phase == "partial-journal":
            stream.write(suffix[:len(suffix)//2]);pause(phase)
        if phase != "witness-ahead":
            stream.write(suffix);os.fsync(stream.fileno());pause("journal-ahead")
    with (root / "mock-witness.json").open("wb", buffering=0) as stream:
        if phase == "partial-witness":
            stream.write(next_witness[:len(next_witness)//2]);pause(phase)
        stream.write(next_witness);os.fsync(stream.fileno());pause("witness-ahead")
    pause("aligned")
"""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate mock witness field")
        result[key] = value
    return result


def _parse_mock(raw: bytes) -> witness.WitnessSnapshot:
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict) or set(value) != {"journal_id", "sequence", "prefix_sha256"}:
            raise ValueError("invalid mock snapshot")
        return witness.WitnessSnapshot(**value)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise witness.WitnessError("invalid or partial mock witness") from exc


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class NativeWitnessProcessExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="trackie-w-witness-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        api = native.NativeWin32(experimental=True)
        _volume, kind, filesystem = api.volume(self.root.anchor)
        if kind != native._DRIVE_FIXED or filesystem != "NTFS":
            self.skipTest("requires fixed NTFS")
        self.env = dict(os.environ)
        if not self.env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
            path = (
                pathlib.Path(native.__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            )
            self.env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(path.read_bytes()).decode()
        self.empty, self.pending = grammar_states()
        self.w0 = witness._head(_ID, self.empty)
        self.w1 = witness._head(_ID, self.pending)
        data = {
            "suffix": base64.b64encode(self.pending[len(self.empty) :]).decode(),
            "next_witness": json.dumps(dataclasses.asdict(self.w1), sort_keys=True),
        }
        self.env["TRACKIE_TEST_WITNESS_DATA"] = json.dumps(data)

    def _stop_at(self, root: pathlib.Path, phase: str) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, phase, str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        assert child.stdout is not None
        stdout = child.stdout
        output: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(output.get(timeout=20).strip(), "READY " + phase)
        finally:
            child.terminate()
            child.wait(timeout=10)
            stdout.close()

    def _inspect_under_real_lock(self, root: pathlib.Path) -> tuple[bytes, bytes, str]:
        module = types.ModuleType("trackie_existing_lock_fixture")
        sys.modules[module.__name__] = module
        source = base64.b64decode(self.env["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
        exec(compile(source, module.__name__ + ".py", "exec"), module.__dict__)  # noqa: S102
        with module._exclusive_archive_sync(root):
            raw_journal = (root / "journal.bin").read_bytes()
            raw_witness = (root / "mock-witness.json").read_bytes()
            try:
                result = witness.compare(_ID, raw_journal, _parse_mock(raw_witness))
            except witness.WitnessError:
                result = "blocked"
            return raw_journal, raw_witness, result

    def test_duplicate_mock_witness_fields_are_rejected(self) -> None:
        raw = (
            b'{"journal_id":"' + _ID.encode() + b'","sequence":0,"sequence":0,'
            b'"prefix_sha256":"' + b"0" * 64 + b'"}'
        )
        with self.assertRaises(witness.WitnessError):
            _parse_mock(raw)

    def test_torn_and_misaligned_updates_stop_after_process_termination(self) -> None:
        for phase in (
            "partial-journal",
            "journal-ahead",
            "witness-ahead",
            "partial-witness",
            "aligned",
        ):
            with self.subTest(phase=phase):
                root = self.root / phase
                root.mkdir()
                (root / ".suno-sync.lock").write_bytes(b"0")
                journal_path, witness_path = root / "journal.bin", root / "mock-witness.json"
                with journal_path.open("xb", buffering=0) as stream:
                    stream.write(self.empty)
                    os.fsync(stream.fileno())
                with witness_path.open("xb", buffering=0) as stream:
                    stream.write(json.dumps(dataclasses.asdict(self.w0), sort_keys=True).encode())
                    os.fsync(stream.fileno())
                self._stop_at(root, phase)
                raw_journal, raw_witness, result = self._inspect_under_real_lock(root)
                self.assertEqual(result, "pending" if phase == "aligned" else "blocked")
                self.assertEqual(
                    self._inspect_under_real_lock(root), (raw_journal, raw_witness, result)
                )


def grammar_states() -> tuple[bytes, bytes]:
    init = {"phase": "multi-format", "version": 1}
    intent = grammar._intent()
    empty = journal.MAGIC + journal._frame(init)
    pending = empty + journal._frame(intent)
    return empty, pending


if __name__ == "__main__":
    unittest.main(verbosity=2)
