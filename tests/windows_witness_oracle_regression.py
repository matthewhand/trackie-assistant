"""Deterministic tests of a hypothetical external witness, not its durability."""

from __future__ import annotations

import hashlib
import unittest

import windows_external_witness_model as witness
import windows_multifile_grammar_regression as grammar
import windows_persistent_journal_experiment as journal

_ID = "a" * 32


def _states() -> tuple[bytes, bytes, bytes]:
    init = {"phase": "multi-format", "version": 1}
    intent = grammar._intent()
    done = {
        "phase": "complete",
        "intent_sha256": hashlib.sha256(journal._record_bytes(intent)).hexdigest(),
    }
    empty = journal.MAGIC + journal._frame(init)
    pending = empty + journal._frame(intent)
    complete = pending + journal._frame(done)
    return empty, pending, complete


class WitnessOracleRegression(unittest.TestCase):
    def test_aligned_heads_and_interrupted_updates(self) -> None:
        empty, pending, complete = _states()
        w0, w1, w2 = (witness._head(_ID, raw) for raw in (empty, pending, complete))
        self.assertEqual(witness.compare(_ID, empty, w0), "empty")
        self.assertEqual(witness.compare(_ID, pending, w1), "pending")
        self.assertEqual(witness.compare(_ID, complete, w2), "completion-frame-visible")
        # If either write is ahead at interruption, the oracle stops.
        for journal_raw, witness_head in (
            (pending, w0),
            (empty, w1),
            (complete, w1),
            (pending, w2),
        ):
            with (
                self.subTest(sequence=witness_head.sequence),
                self.assertRaises(witness.WitnessError),
            ):
                witness.compare(_ID, journal_raw, witness_head)
        with self.assertRaises(witness.WitnessError):
            witness.compare(_ID, pending[:-3], w1)

    def test_same_length_different_intent_and_journal_id_rejected(self) -> None:
        empty, pending, _ = _states()
        expected = witness._head(_ID, pending)
        other = grammar._intent()
        other["files"][0]["sha256"] = "f" * 64
        changed = empty + journal._frame(other)
        with self.assertRaises(witness.WitnessError):
            witness.compare(_ID, changed, expected)
        with self.assertRaises(witness.WitnessError):
            witness.compare("b" * 32, pending, expected)

    def test_coordinated_rollback_is_not_detectable(self) -> None:
        _empty, pending, complete = _states()
        w1, w2 = witness._head(_ID, pending), witness._head(_ID, complete)
        with self.assertRaises(witness.WitnessError):
            witness.compare(_ID, pending, w2)
        # A witness rolled back together with its journal is indistinguishable.
        self.assertEqual(witness.compare(_ID, pending, w1), "pending")

    def test_stale_snapshot_can_mask_a_rollback(self) -> None:
        _empty, pending, complete = _states()
        stale = witness._head(_ID, pending)
        latest = witness._head(_ID, complete)
        with self.assertRaises(witness.WitnessError):
            witness.compare(_ID, pending, latest)
        # Supplying an earlier cached response hides the later trusted head.
        self.assertEqual(witness.compare(_ID, pending, stale), "pending")

    def test_forged_external_snapshot_is_not_authenticated(self) -> None:
        _empty, pending, _complete = _states()
        forged = witness._head(_ID, pending)
        self.assertEqual(witness.compare(_ID, pending, forged), "pending")

    def test_malformed_snapshots_are_rejected(self) -> None:
        empty, _pending, _complete = _states()
        valid = witness._head(_ID, empty)
        values = (
            witness.WitnessSnapshot(_ID, True, valid.prefix_sha256),
            witness.WitnessSnapshot(_ID, 4, valid.prefix_sha256),
            witness.WitnessSnapshot(_ID, 0, "g" * 64),
            witness.WitnessSnapshot("A" * 32, 0, valid.prefix_sha256),
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(witness.WitnessError):
                witness.compare(_ID, empty, value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
