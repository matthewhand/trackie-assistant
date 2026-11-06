"""Read-only research oracle for a purported external Windows journal witness.

NOT a witness service, trusted provisioning, transaction coordinator, or
production recovery protocol. The caller must obtain an authenticated,
durable, monotonic witness from a genuinely independent trust domain, require fresh
linearizable reads and compare-and-advance, and keep noncooperating writers
stopped. This oracle cannot authenticate or establish freshness of the supplied
snapshot, attest a lock, ensure file or directory durability, or detect a
coordinated rollback of journal and witness. It never repairs or mutates data.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import windows_bound_multifile_model as multi

_MAX = 32768
_DOMAIN = b"TRACKIE-WITNESS-RESEARCH-v1\0"


class WitnessError(RuntimeError):
    """The journal and purported witness cannot be safely reconciled."""


@dataclass(frozen=True)
class WitnessSnapshot:
    journal_id: str
    sequence: int
    prefix_sha256: str


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    )


def _head(journal_id: str, raw: bytes) -> WitnessSnapshot:
    """Compute the canonical prefix head; this is not witness authentication."""
    if not _valid_id(journal_id) or not isinstance(raw, bytes) or len(raw) > _MAX:
        raise WitnessError("Invalid journal identifier or size")
    try:
        records = multi._parse_multi(raw)
    except multi.BindingError as exc:
        raise WitnessError("Journal is malformed or incomplete") from exc
    digest = hashlib.sha256(_DOMAIN + journal_id.encode("ascii") + b"\0" + raw).hexdigest()
    return WitnessSnapshot(journal_id, len(records), digest)


def compare(journal_id: str, raw: bytes, trusted_snapshot: WitnessSnapshot) -> str:
    """Compare one journal snapshot; no completion is durably acknowledged."""
    if (
        not isinstance(trusted_snapshot, WitnessSnapshot)
        or not _valid_id(trusted_snapshot.journal_id)
        or type(trusted_snapshot.sequence) is not int
        or trusted_snapshot.sequence not in (0, 1, 2)
        or not isinstance(trusted_snapshot.prefix_sha256, str)
        or len(trusted_snapshot.prefix_sha256) != 64
        or any(c not in "0123456789abcdef" for c in trusted_snapshot.prefix_sha256)
    ):
        raise WitnessError("Invalid purported witness snapshot")
    actual = _head(journal_id, raw)
    if (
        actual.journal_id != trusted_snapshot.journal_id
        or actual.sequence != trusted_snapshot.sequence
        or not hmac.compare_digest(actual.prefix_sha256, trusted_snapshot.prefix_sha256)
    ):
        raise WitnessError("Journal and independent witness differ; stop and inspect")
    return ("empty", "pending", "completion-frame-visible")[actual.sequence]
