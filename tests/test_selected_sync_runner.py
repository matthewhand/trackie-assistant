"""Selected-job accounting, duplicate prevention and audit acceptance."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from suno_dl.sync_acceptance import verify_terminal_success

IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
]
RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.parametrize(
    ("unavailable", "scope"),
    [(False, "normal"), (True, "normal"), (False, "resume"), (False, "drift")],
)
async def test_selected_job_deduplicates_preserves_existing_and_reports_truthfully(
    tmp_path, monkeypatch, unavailable, scope
):
    script = Path(__file__).parents[1] / "deploy/ct107/sync_selected.py"
    spec = importlib.util.spec_from_file_location("selected_job", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "library"
    root.mkdir()
    (root / ".library").mkdir()
    old = root / "old.m4a"
    old.write_bytes(b"original recording")
    entries = {IDS[0]: NS(media=(old,))}
    calls = []

    class Index:
        conflicts = {}

        def __init__(self, *_args):
            self.entries = {}

        def scan(self):
            self.entries = dict(entries)
            return self

        def get(self, cid):
            return self.entries.get(cid)

    class Fetcher:
        def __init__(self, *_args, **_kwargs):
            self.client = NS(event_hooks={"response": []})
            self.rate_limiter = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    async def api(playlist_id, _page, **_kwargs):
        return {"name": playlist_id}

    async def membership(page, ref, **_kwargs):
        await page(ref, 1)
        selected = IDS[:2] if ref.id == IDS[0] else IDS[1:]
        return tuple(NS(id=cid, title="Synthetic track") for cid in selected)

    async def download(tracks, *_args, **options):
        cid = tracks[0].id
        calls.append(cid)
        assert options["covers"] and options["extract_audio"] and options["audio_only"]
        if unavailable and cid == IDS[2]:
            raise module.ProducerLookupError("No verified producer")
        media = root / (cid + ".m4a")
        cover = root / (cid + ".cover.jpg")
        side = root / (cid + ".json")
        media.write_bytes(b"new synthetic recording")
        cover.write_bytes(b"synthetic image")
        side.write_text(
            json.dumps(
                {
                    "artist": "Producer",
                    "creator_handle": "producer",
                    "saved_files": [media.name, cover.name],
                }
            )
        )
        entries[cid] = NS(media=(media,), sidecar=side)
        return {"downloaded": 1, "skipped_exists": 0}

    monkeypatch.setattr(module, "LibraryIndex", Index)
    monkeypatch.setattr(module, "Fetcher", Fetcher)
    monkeypatch.setattr(module, "fetch_public_playlist_api_page", api)
    monkeypatch.setattr(module, "_fetch_public_api_membership", membership)
    monkeypatch.setattr(module, "download_tracks", download)
    monkeypatch.setattr(module, "require_owned_library", lambda *_args: None)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    audit = audit_dir / "run.jsonl"
    options = {}
    if scope != "normal":
        options["expected_missing"] = frozenset(IDS[1:] if scope == "resume" else IDS[1:2])
    if scope == "drift":
        with pytest.raises(ValueError, match="approved resume set"):
            await module.run(root, audit, RUN, IDS[:2], tmp_path / "absent-blocklist", **options)
        assert calls == []
        assert old.read_bytes() == b"original recording"
        assert not list(root.glob("*.json"))
        return
    result = await module.run(root, audit, RUN, IDS[:2], tmp_path / "absent-blocklist", **options)
    assert result == int(unavailable)
    assert calls == IDS[1:]
    assert old.read_bytes() == b"original recording"
    status = json.loads((root / ".library/sync-status.json").read_text())
    assert status["capacity"]["queue"]["count"] == 0
    assert status["capacity"]["scope"] == "Selected playlists"
    assert status["remaining"] == 0
    assert status["downloaded"] == (1 if unavailable else 2)
    assert status["phase"] == ("partial" if unavailable else "complete")
    if not unavailable:
        container = "ab" * 32
        proof = verify_terminal_success(
            audit,
            RUN,
            container,
            {"Id": container, "State": {"Status": "exited", "Running": False, "ExitCode": 0}},
        )
        assert len(proof["downloads"]) == 2
    before = audit.read_bytes()
    with pytest.raises(FileExistsError):
        await module.run(root, audit, RUN, IDS[:2], tmp_path / "absent-blocklist")
    assert audit.read_bytes() == before
