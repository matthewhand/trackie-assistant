"""Unintegrated Windows two-file crash-recovery experiment.

NOT a production protocol or power-loss guarantee. Requires a trusted,
preprovisioned external journal binding and a multi-file format record,
honest file flushes, stopped
noncooperating writers, and a fresh flat fixed-NTFS test archive. It uses
unkeyed record digests and does not detect coordinated journal rollback.
Python lock ownership is not attested against same-process manipulation.
Group publication is not atomically visible: media may appear before sidecar,
and a crash or namespace reordering may expose sidecar before media.
No existing-library import, overwrite, cleanup, multi-operation ledger,
physical-locality proof or trusted provisioning/witness is implemented.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

import windows_bound_publish_model as single
import windows_filesystem_adapter_model as native
import windows_persistent_journal_experiment as journal
import windows_recovery_model as recovery

BindingError = single.BindingError
ExternalBinding = single.ExternalBinding
_NAMES = (
    ("media", ".stage-media", "media.wav"),
    ("sidecar", ".stage-sidecar", "media.json"),
)
_MAX = 2 * 1024 * 1024


def _sidecar_bytes(clip_id: str) -> bytes:
    return (
        json.dumps({"id": clip_id, "saved_files": ["media.wav"]}, sort_keys=True) + "\n"
    ).encode()


def _parse_multi(raw: bytes) -> list[dict[str, Any]]:
    if not raw.startswith(journal.MAGIC):
        raise BindingError("Invalid journal header")
    records: list[dict[str, Any]] = []
    try:
        for line in raw[len(journal.MAGIC) :].splitlines(keepends=True):
            if not line.endswith(b"\n") or len(line) > 8192:
                raise ValueError("partial or oversized frame")
            body, checksum = line[:-1].rsplit(b"|", 1)
            if hashlib.sha256(body).hexdigest().encode("ascii") != checksum:
                raise ValueError("record checksum differs")
            value = json.loads(body, object_pairs_hook=journal._unique)
            if not isinstance(value, dict) or journal._record_bytes(value) != body:
                raise ValueError("noncanonical record")
            records.append(value)
        if (
            not records
            or len(records) > 3
            or records[0] != {"phase": "multi-format", "version": 1}
            or type(records[0]["version"]) is not int
        ):
            raise ValueError("missing or invalid preprovisioned multi-file format")
        records = records[1:]
        if not records:
            return records
        intent = records[0]
        if (
            set(intent) != {"phase", "version", "id", "files"}
            or intent["phase"] != "multi-intent"
            or type(intent["version"]) is not int
            or intent["version"] != 1
            or not isinstance(intent["id"], str)
            or str(uuid.UUID(intent["id"])) != intent["id"]
            or not isinstance(intent["files"], list)
            or len(intent["files"]) != 2
        ):
            raise ValueError("invalid intent")
        for value, (role, stage, target) in zip(intent["files"], _NAMES, strict=True):
            if (
                not isinstance(value, dict)
                or set(value) != {"role", "stage", "target", "identity", "sha256"}
                or value["role"] != role
                or value["stage"] != stage
                or value["target"] != target
            ):
                raise ValueError("invalid member")
            identity, digest = value["identity"], value["sha256"]
            if (
                not isinstance(identity, list)
                or len(identity) != 3
                or any(type(v) is not int or v < 0 for v in identity)
                or identity[1] == 0
                or identity[2] > _MAX
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("invalid member identity")
        sidecar = _sidecar_bytes(intent["id"])
        if intent["files"][1]["sha256"] != hashlib.sha256(sidecar).hexdigest() or intent["files"][
            1
        ]["identity"][2] != len(sidecar):
            raise ValueError("sidecar does not match clip identity")
        if len(records) == 2:
            expected = {
                "phase": "complete",
                "intent_sha256": hashlib.sha256(journal._record_bytes(intent)).hexdigest(),
            }
            if records[1] != expected:
                raise ValueError("invalid completion")
        return records
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise BindingError("Invalid multi-file journal; stop and inspect") from exc


class MultiFilePublisher(single.BoundPublisher):
    """Test-only subclass; name-mangled lock access precludes production reuse."""

    def _multi_records(self) -> list[dict[str, Any]]:
        try:
            allowed = {
                recovery.LOCK,
                "journal.bin",
                *(entry for triple in _NAMES for entry in triple[1:]),
            }
            if any(path.name not in allowed for path in self.root.iterdir()):
                raise ValueError("unexplained archive member")
            return _parse_multi(self._read_journal())
        except (OSError, ValueError) as exc:
            raise BindingError("Cannot inspect archive") from exc

    def _states(self, intent: dict[str, Any]) -> list[str]:
        states = []
        for item in intent["files"]:
            stage, target = self.root / item["stage"], self.root / item["target"]
            stage_exists, target_exists = os.path.lexists(stage), os.path.lexists(target)
            if stage_exists == target_exists:
                raise BindingError("Missing or conflicting member")
            selected = stage if stage_exists else target
            self._verified_file(selected, item, flush=False)
            states.append("stage" if stage_exists else "target")
        return states

    def classify(self) -> str:
        records = self._multi_records()
        if not records:
            if any(os.path.lexists(self.root / name) for triple in _NAMES for name in triple[1:]):
                raise BindingError("Unjournalled member requires inspection")
            return "empty"
        states = self._states(records[0])
        if len(records) == 2:
            if states != ["target", "target"]:
                raise BindingError("Completion conflicts with members")
            return "completion-frame-visible"
        return "pending-" + "-".join(states)

    def begin(self, media: bytes, hook: Callable[[str], None] = lambda _phase: None) -> str:
        if not isinstance(media, bytes) or len(media) > _MAX or self.classify() != "empty":
            raise BindingError("Fresh archive and bounded fixture media are required")
        clip_id = str(uuid.uuid4())
        sidecar = _sidecar_bytes(clip_id)
        members = []
        for (role, stage_name, target_name), payload in zip(_NAMES, (media, sidecar), strict=True):
            try:
                with (self.root / stage_name).open("xb", buffering=0) as stream:
                    hook(role + "_stage_created")
                    if stream.write(payload) != len(payload):
                        raise BindingError("Short stage write")
                    hook(role + "_stage_written")
                    os.fsync(stream.fileno())
                    info = os.fstat(stream.fileno())
                    hook(role + "_stage_flushed")
            except OSError as exc:
                raise BindingError("Stage write or flush failed") from exc
            members.append(
                {
                    "role": role,
                    "stage": stage_name,
                    "target": target_name,
                    "identity": list(recovery._identity(info)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        intent = {"phase": "multi-intent", "version": 1, "id": clip_id, "files": members}
        self._append(intent, "intent", hook)
        return self.recover(hook)

    def recover(self, hook: Callable[[str], None] = lambda _phase: None) -> str:
        records = self._multi_records()
        observed = self.classify()
        if observed == "empty":
            raise BindingError("No pending operation")
        # Stop before any publication if any member or the journal is unsafe.
        self._sync_journal()
        intent = records[0]
        states = self._states(intent)
        # Flush every verified member before the first new publication. A
        # known later-member flush failure must not expose an earlier member.
        for item, state in zip(intent["files"], states, strict=True):
            selected = self.root / item["stage" if state == "stage" else "target"]
            self._verified_file(selected, item, flush=True)
        if observed != "completion-frame-visible":
            for item, state in zip(intent["files"], states, strict=True):
                if state == "stage":
                    source, target = self.root / item["stage"], self.root / item["target"]
                    self._verified_file(source, item, flush=False)
                    lock = self._BoundPublisher__lock
                    lock.verify()
                    native.Adapter(lock.api, archive_lock_held=True).publish(
                        str(source), str(target), archive_lock_held=True, staged_file_owned=True
                    )
                    hook(item["role"] + "_published")
        # Recheck both before acknowledging any complete frame.
        for item in intent["files"]:
            self._verified_file(self.root / item["target"], item, flush=True)
        if observed != "completion-frame-visible":
            done = {
                "phase": "complete",
                "intent_sha256": hashlib.sha256(journal._record_bytes(intent)).hexdigest(),
            }
            self._append(done, "complete", hook)
        return "complete"
