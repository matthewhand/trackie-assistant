"""Platform-independent grammar checks for the unintegrated multi-file model."""

import hashlib
import unittest
import uuid

import windows_bound_multifile_model as model
import windows_persistent_journal_experiment as journal


def _intent() -> dict:
    clip_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
    sidecar = model._sidecar_bytes(clip_id)
    return {
        "phase": "multi-intent",
        "version": 1,
        "id": clip_id,
        "files": [
            {
                "role": "media",
                "stage": ".stage-media",
                "target": "media.wav",
                "identity": [1, 2, 3],
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            },
            {
                "role": "sidecar",
                "stage": ".stage-sidecar",
                "target": "media.json",
                "identity": [1, 3, len(sidecar)],
                "sha256": hashlib.sha256(sidecar).hexdigest(),
            },
        ],
    }


def _wire(*records: dict) -> bytes:
    return journal.MAGIC + b"".join(journal._frame(record) for record in records)


class MultiFileGrammarRegression(unittest.TestCase):
    def test_valid_multi_format_intent_and_completion(self):
        init = {"phase": "multi-format", "version": 1}
        intent = _intent()
        done = {
            "phase": "complete",
            "intent_sha256": hashlib.sha256(journal._record_bytes(intent)).hexdigest(),
        }
        self.assertEqual(model._parse_multi(_wire(init)), [])
        self.assertEqual(model._parse_multi(_wire(init, intent, done)), [intent, done])

    def test_invalid_multifile_records_are_rejected(self):
        for mutation in (
            "missing-format",
            "partial",
            "checksum",
            "bool-version",
            "role",
            "sidecar",
            "extra",
        ):
            with self.subTest(mutation=mutation):
                init = {"phase": "multi-format", "version": 1}
                intent = _intent()
                if mutation == "missing-format":
                    raw = _wire(intent)
                elif mutation == "partial":
                    raw = _wire(init, intent)[:-3]
                elif mutation == "checksum":
                    valid = _wire(init, intent)
                    changed = b"1" if valid[-2:-1] != b"1" else b"2"
                    raw = valid[:-2] + changed + b"\n"
                elif mutation == "bool-version":
                    raw = _wire({"phase": "multi-format", "version": True})
                elif mutation == "role":
                    intent["files"][1]["role"] = "media"
                    raw = _wire(init, intent)
                elif mutation == "sidecar":
                    intent["files"][1]["sha256"] = "0" * 64
                    raw = _wire(init, intent)
                else:
                    raw = _wire(init, intent, {"phase": "complete", "intent_sha256": "0" * 64})
                with self.assertRaises(model.BindingError):
                    model._parse_multi(raw)

    def test_duplicate_json_keys_are_rejected_with_valid_checksum(self):
        body = b'{"phase":"multi-format","phase":"multi-format","version":1}'
        raw = journal.MAGIC + body + b"|" + hashlib.sha256(body).hexdigest().encode() + b"\n"
        with self.assertRaises(model.BindingError):
            model._parse_multi(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
