"""Disposable, stdlib-only Windows persistent-journal process experiment.

This is not production code, a filesystem durability proof, or a complete
multi-file recovery protocol. It assumes a trusted, pre-existing journal that is
never renamed or deleted, honest successful file flushes, all writers using the
same archive lock, trusted private state and a fresh single-file test archive.
Torn/invalid tails and unexplained paths refuse; no truncation or repair occurs.
A whole valid COMPLETE suffix can be lost or rolled back indistinguishably from
an incomplete operation without an independent durable monotonic witness.
Same-user coordinated state rewriting and concurrent uncooperative writers are
outside this isolated experiment's trust boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest

import windows_filesystem_adapter_model as native

MAGIC = b"TRACKIE-RESEARCH-ONLY-1\n"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _record_bytes(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")


def _frame(record: dict) -> bytes:
    body = _record_bytes(record)
    return body + b"|" + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n"


def _parse(data: bytes) -> list[dict]:
    if not data.startswith(MAGIC):
        raise ValueError("bad journal header")
    result = []
    for line in data[len(MAGIC) :].splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > 8192:
            raise ValueError("partial or oversized record")
        try:
            body, checksum = line[:-1].rsplit(b"|", 1)
            if hashlib.sha256(body).hexdigest().encode("ascii") != checksum:
                raise ValueError("bad record checksum")
            record = json.loads(body, object_pairs_hook=_unique)
            if not isinstance(record, dict) or _record_bytes(record) != body:
                raise ValueError("noncanonical record")
            result.append(record)
        except (ValueError, UnicodeError, TypeError) as exc:
            raise ValueError("invalid record") from exc
    if len(result) > 2:
        raise ValueError("unexpected records")
    if result:
        pending = result[0]
        if set(pending) != {"phase", "stage", "target", "identity", "sha256"}:
            raise ValueError("invalid intent fields")
        if (
            pending["phase"] != "pending"
            or pending["stage"] != "stage.bin"
            or pending["target"] != "target.bin"
        ):
            raise ValueError("invalid intent")
        if len(result) == 2:
            done = result[1]
            if done != {
                "phase": "complete",
                "intent_sha256": hashlib.sha256(_record_bytes(pending)).hexdigest(),
            }:
                raise ValueError("invalid completion")
    return result


def _verified_stat(path: pathlib.Path, limit: int) -> os.stat_result:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("cannot safely inspect file") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not before.st_ino
        or getattr(before, "st_file_attributes", 0) & native._ATTR_REPARSE
        or before.st_size > limit
    ):
        raise ValueError("unsafe file, link or reparse point")
    return before


def _verified_bytes(path: pathlib.Path, limit: int) -> tuple[bytes, os.stat_result]:
    before = _verified_stat(path, limit)
    identity = (before.st_dev, before.st_ino, before.st_size)
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != identity:
                raise ValueError("file changed while opening")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (after.st_dev, after.st_ino, after.st_size) != identity:
                raise ValueError("file changed while reading")
        final = _verified_stat(path, limit)
        if (final.st_dev, final.st_ino, final.st_size) != identity or len(raw) != before.st_size:
            raise ValueError("file changed after reading")
        return raw, before
    except OSError as exc:
        raise ValueError("cannot safely inspect file") from exc


def _reconcile(root: pathlib.Path, expected_journal_identity: tuple[int, int]) -> str:
    """Classify this controlled fixture while the caller holds the common lock.

    The expected identity must be kept independently of the journal. A live
    handle, ACL enforcement and a durable identity witness are not modeled.
    """
    allowed = {".suno-sync.lock", "journal.bin", "stage.bin", "target.bin"}
    if any(path.name not in allowed for path in root.iterdir()):
        raise ValueError("unexplained archive member")
    _verified_stat(root / ".suno-sync.lock", 8192)
    raw_journal, journal_info = _verified_bytes(root / "journal.bin", 32768)
    if (journal_info.st_dev, journal_info.st_ino) != expected_journal_identity:
        raise ValueError("journal identity differs from external expectation")
    records = _parse(raw_journal)
    stage, target = root / "stage.bin", root / "target.bin"
    stage_exists = os.path.lexists(stage)
    target_exists = os.path.lexists(target)
    if not records:
        if stage_exists or target_exists:
            raise ValueError("unexplained output")
        return "empty"
    pending = records[0]
    if stage_exists == target_exists:
        raise ValueError("missing or conflicting outputs")
    selected = stage if stage_exists else target
    raw, info = _verified_bytes(selected, 2 * 1024 * 1024)
    if info.st_nlink != 1 or [info.st_dev, info.st_ino, info.st_size] != pending["identity"]:
        raise ValueError("unexpected file identity")
    if hashlib.sha256(raw).hexdigest() != pending["sha256"]:
        raise ValueError("unexpected file bytes")
    if len(records) == 2:
        if selected != target:
            raise ValueError("completion without published file")
        return "complete"
    return "pending-stage" if selected == stage else "pending-target"


_CHILD = r"""
import base64, hashlib, json, os, pathlib, sys, time, types
for name, key in (("windows_filesystem_adapter_model", "TRACKIE_NATIVE_MODEL"),
                  ("trackie_lock", "TRACKIE_ARCHIVE_LOCK_MODEL")):
    module = types.ModuleType(name); sys.modules[name] = module
    exec(compile(base64.b64decode(os.environ[key]).decode(), name + ".py", "exec"), module.__dict__)
import windows_filesystem_adapter_model as native
lock = sys.modules["trackie_lock"]
phase, root = sys.argv[1], pathlib.Path(sys.argv[2])
def encode(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
def frame(record):
    body = encode(record)
    return body + b"|" + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n"
def pause(point):
    if phase == point:
        print("READY " + point, flush=True); time.sleep(300)
with lock._exclusive_archive_sync(root):
    stage, target, journal = root / "stage.bin", root / "target.bin", root / "journal.bin"
    with stage.open("xb", buffering=0) as stream:
        payload = b"persistent-journal-sentinel"
        stream.write(payload); os.fsync(stream.fileno())
        st = os.fstat(stream.fileno())
    pending = {"phase":"pending", "stage":"stage.bin", "target":"target.bin",
               "identity":[st.st_dev,st.st_ino,st.st_size],
               "sha256":hashlib.sha256(payload).hexdigest()}
    pending_frame = frame(pending)
    with journal.open("ab", buffering=0) as output:
        if phase == "partial-intent":
            output.write(pending_frame[:len(pending_frame)//2]); pause(phase)
        output.write(pending_frame); os.fsync(output.fileno()); pause("intent")
        native.Adapter(native.NativeWin32(experimental=True), archive_lock_held=True).publish(
            str(stage), str(target), archive_lock_held=True, staged_file_owned=True)
        pause("publication")
        done = {"phase":"complete", "intent_sha256":hashlib.sha256(encode(pending)).hexdigest()}
        done_frame = frame(done)
        if phase == "partial-complete":
            output.write(done_frame[:len(done_frame)//2]); pause(phase)
        output.write(done_frame); os.fsync(output.fileno()); pause("complete")
"""


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class PersistentJournalExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="trackie-persistent-journal-")
        self.addCleanup(self.temp.cleanup)
        api = native.NativeWin32(experimental=True)
        h = api.open(self.temp.name, directory=True, writable=False)
        try:
            self.root = pathlib.Path(native._canonical_name(api.final_path(h)))
        finally:
            api.close(h)
        _volume, kind, filesystem = api.volume(self.root.anchor)
        if kind != native._DRIVE_FIXED or filesystem != "NTFS":
            self.skipTest("requires fixed NTFS")
        self.env = dict(os.environ)
        if not self.env.get("TRACKIE_NATIVE_MODEL"):
            self.env["TRACKIE_NATIVE_MODEL"] = base64.b64encode(
                pathlib.Path(native.__file__).read_bytes()
            ).decode()
        if not self.env.get("TRACKIE_ARCHIVE_LOCK_MODEL"):
            path = (
                pathlib.Path(native.__file__).resolve().parents[1] / "src/suno_dl/archive_lock.py"
            )
            self.env["TRACKIE_ARCHIVE_LOCK_MODEL"] = base64.b64encode(path.read_bytes()).decode()

    def _checked(self, root: pathlib.Path, journal_id: tuple[int, int]) -> str:
        existing = types.ModuleType("trackie_existing_lock_fixture")
        sys.modules[existing.__name__] = existing
        source = base64.b64decode(self.env["TRACKIE_ARCHIVE_LOCK_MODEL"]).decode("utf-8")
        exec(compile(source, existing.__name__ + ".py", "exec"), existing.__dict__)  # noqa: S102
        with existing._exclusive_archive_sync(root):
            return _reconcile(root, journal_id)

    def _child_stop(self, root: pathlib.Path, phase: str) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD, phase, str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        stdout = process.stdout
        assert stdout is not None
        output: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: output.put(stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(output.get(timeout=20).strip(), "READY " + phase)
        finally:
            process.terminate()
            process.wait(timeout=10)
            stdout.close()

    def test_crash_cuts_and_readonly_reconciliation(self) -> None:
        for phase in ("partial-intent", "intent", "publication", "partial-complete", "complete"):
            with self.subTest(phase=phase):
                root = self.root / phase
                root.mkdir()
                (root / ".suno-sync.lock").write_bytes(b"0")
                journal = root / "journal.bin"
                with journal.open("xb", buffering=0) as stream:
                    stream.write(MAGIC)
                    os.fsync(stream.fileno())
                initial = journal.stat()
                journal_id = (initial.st_dev, initial.st_ino)
                self._child_stop(root, phase)
                self.assertEqual(
                    (journal.stat().st_dev, journal.stat().st_ino), (initial.st_dev, initial.st_ino)
                )
                if phase in ("partial-intent", "partial-complete"):
                    with self.assertRaises(ValueError):
                        self._checked(root, journal_id)
                else:
                    self.assertEqual(
                        self._checked(root, journal_id),
                        {
                            "intent": "pending-stage",
                            "publication": "pending-target",
                            "complete": "complete",
                        }[phase],
                    )
                if phase == "complete":
                    (root / "target.bin").rename(root / "displaced.bin")
                    with self.assertRaises(ValueError):
                        self._checked(root, journal_id)
                    self.assertEqual(
                        (root / "displaced.bin").read_bytes(), b"persistent-journal-sentinel"
                    )

    def test_header_only_rejects_unexplained_archive_member(self) -> None:
        root = self.root / "extra"
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        (root / "journal.bin").write_bytes(MAGIC)
        (root / "unexplained.m4a").write_bytes(b"existing bytes")
        info = (root / "journal.bin").stat()
        with self.assertRaises(ValueError):
            self._checked(root, (info.st_dev, info.st_ino))
        self.assertEqual((root / "unexplained.m4a").read_bytes(), b"existing bytes")

    def test_reparse_stage_pointing_to_same_inode_is_rejected(self) -> None:
        root = self.root / "reparse"
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        with (root / "journal.bin").open("xb", buffering=0) as stream:
            stream.write(MAGIC)
            os.fsync(stream.fileno())
        info = (root / "journal.bin").stat()
        journal_id = (info.st_dev, info.st_ino)
        self._child_stop(root, "intent")
        original, link = self.root / "outside-stage", root / "stage.bin"
        link.rename(original)
        try:
            os.symlink(original, link)
        except OSError as exc:
            self.skipTest(f"creating a disposable symlink is unavailable: {exc}")
        with self.assertRaises(ValueError):
            self._checked(root, journal_id)
        self.assertEqual(original.read_bytes(), b"persistent-journal-sentinel")

    def test_missing_or_replaced_journal_fails_external_identity_check(self) -> None:
        root = self.root / "journal-impostor"
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        journal = root / "journal.bin"
        journal.write_bytes(MAGIC)
        initial = journal.stat()
        journal_id = (initial.st_dev, initial.st_ino)
        self._child_stop(root, "intent")
        original = self.root / "original-journal"
        journal.rename(original)
        with self.assertRaises(ValueError):
            self._checked(root, journal_id)
        journal.write_bytes(original.read_bytes())
        self.assertNotEqual(journal.stat().st_ino, initial.st_ino)
        with self.assertRaises(ValueError):
            self._checked(root, journal_id)
        self.assertEqual(journal.read_bytes(), original.read_bytes())

    def test_journal_size_is_bounded_before_read(self) -> None:
        root = self.root / "oversized"
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        journal = root / "journal.bin"
        journal.write_bytes(MAGIC + b"x" * 32769)
        info = journal.stat()
        with self.assertRaises(ValueError):
            self._checked(root, (info.st_dev, info.st_ino))

    def test_whole_completion_rollback_is_not_detectable(self) -> None:
        root = self.root / "rollback"
        root.mkdir()
        (root / ".suno-sync.lock").write_bytes(b"0")
        journal = root / "journal.bin"
        with journal.open("xb", buffering=0) as stream:
            stream.write(MAGIC)
            os.fsync(stream.fileno())
        info = journal.stat()
        journal_id = (info.st_dev, info.st_ino)
        self._child_stop(root, "complete")
        self.assertEqual(self._checked(root, journal_id), "complete")
        lines = journal.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 3)
        with journal.open("r+b", buffering=0) as stream:
            stream.seek(len(lines[0]) + len(lines[1]))
            stream.truncate()
            os.fsync(stream.fileno())
        # This observation is a limitation, not proof that rollback is safe.
        self.assertEqual(self._checked(root, journal_id), "pending-target")
        self.assertEqual((root / "target.bin").read_bytes(), b"persistent-journal-sentinel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
