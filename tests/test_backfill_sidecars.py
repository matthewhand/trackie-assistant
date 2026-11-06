"""scripts/backfill_sidecars.py: matching, ambiguity, no-overwrite, dry-run."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_sidecars.py"


@pytest.fixture(scope="module")
def bf() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_sidecars", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _id(n: int) -> str:
    return f"{n:08x}-1111-4111-8111-111111111111"


def _clip(n: int, title: str, **kw) -> dict:
    base = {
        "id": _id(n),
        "title": title,
        "handle": "drinko",
        "is_public": True,
        "created_at": "2026-01-02T03:04:05.000Z",
        "model_name": "chirp-v4",
        "major_model_version": "v4",
        "duration": 123.9,
        "tags": "synthwave",
    }
    base.update(kw)
    return base


def _touch(d: Path, name: str) -> Path:
    p = d / name
    p.write_bytes(b"x" * 2048)
    return p


def _sidecar(d: Path, stem: str) -> dict:
    return json.loads((d / f"{stem}.json").read_text(encoding="utf-8"))


def test_matches_both_feat_forms_and_special_chars(bf: ModuleType, tmp_path: Path) -> None:
    clips = [
        _clip(1, "Doomsday (feat. Yawgmoth & Hans Jonas)"),
        _clip(2, "Old (feat. Alice)"),
        _clip(3, "Überwanker: 1/2?"),
    ]
    _touch(tmp_path, "drinko_Doomsday (feat_ Yawgmoth & Hans Jonas).m4a")  # current form
    _touch(tmp_path, "drinko_Old (feat. Alice).m4a")  # pre-1.6 form
    _touch(tmp_path, "drinko_Old (feat. Alice).mp4")  # video shares the stem
    _touch(tmp_path, "drinko_Überwanker_1_2.m4a")
    report = bf.backfill(tmp_path, clips)
    assert report["sidecars_written"] == 3
    assert report["clips_unmatched"] == 0 and report["files_unmatched"] == 0
    d = _sidecar(tmp_path, "drinko_Doomsday (feat_ Yawgmoth & Hans Jonas)")
    assert d["id"] == _id(1) and d["title"] == "Doomsday (feat. Yawgmoth & Hans Jonas)"
    assert d["source"] == "backfill" and d["model_version"] == "v4" and d["duration_sec"] == 123
    old = _sidecar(tmp_path, "drinko_Old (feat. Alice)")
    assert old["saved_files"] == ["drinko_Old (feat. Alice).m4a", "drinko_Old (feat. Alice).mp4"]
    assert _sidecar(tmp_path, "drinko_Überwanker_1_2")["title"] == "Überwanker: 1/2?"


def test_ambiguous_same_title_clips_get_no_sidecar(bf: ModuleType, tmp_path: Path) -> None:
    clips = [_clip(1, "Finest Hour"), _clip(2, "Finest Hour"), _clip(3, "Solo")]
    _touch(tmp_path, "drinko_Finest Hour.m4a")
    _touch(tmp_path, "drinko_Finest Hour_2.m4a")
    _touch(tmp_path, "drinko_Solo.m4a")
    report = bf.backfill(tmp_path, clips)
    assert report["sidecars_written"] == 1
    assert {a["id"] for a in report["ambiguous"]} == {_id(1), _id(2)}
    assert set(report["ambiguous_stems"]) == {"drinko_Finest Hour", "drinko_Finest Hour_2"}
    assert not (tmp_path / "drinko_Finest Hour.json").exists()
    assert not (tmp_path / "drinko_Finest Hour_2.json").exists()
    assert (tmp_path / "drinko_Solo.json").exists()


def test_exact_file_field_resolves_ambiguity(bf: ModuleType, tmp_path: Path) -> None:
    clips = [
        _clip(1, "Finest Hour", file="drinko_Finest Hour.m4a"),
        _clip(2, "Finest Hour", file="drinko_Finest Hour_2.m4a"),
    ]
    _touch(tmp_path, "drinko_Finest Hour.m4a")
    _touch(tmp_path, "drinko_Finest Hour_2.m4a")
    report = bf.backfill(tmp_path, clips)
    assert report["sidecars_written"] == 2 and report["clips_ambiguous"] == 0
    assert _sidecar(tmp_path, "drinko_Finest Hour_2")["id"] == _id(2)


def test_private_clips_ignored_and_orphans_reported(bf: ModuleType, tmp_path: Path) -> None:
    clips = [_clip(1, "Public"), _clip(2, "Secret", is_public=False)]
    _touch(tmp_path, "drinko_Public.m4a")
    _touch(tmp_path, "drinko_Secret.m4a")
    _touch(tmp_path, "drinko_0.m4a")
    report = bf.backfill(tmp_path, clips)
    assert report["clips_private_ignored"] == 1
    assert report["sidecars_written"] == 1
    assert set(report["unmatched_files"]) == {"drinko_Secret", "drinko_0"}
    assert not (tmp_path / "drinko_Secret.json").exists()


def test_never_overwrites_and_never_touches_media(bf: ModuleType, tmp_path: Path) -> None:
    audio = _touch(tmp_path, "drinko_Song.m4a")
    before = audio.read_bytes()
    (tmp_path / "drinko_Song.json").write_text('{"keep": "me"}', encoding="utf-8")
    report = bf.backfill(tmp_path, [_clip(1, "Song")])
    assert report["sidecars_written"] == 0 and report["sidecars_already_present"] == 1
    assert json.loads((tmp_path / "drinko_Song.json").read_text()) == {"keep": "me"}
    assert audio.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["drinko_Song.json", "drinko_Song.m4a"]


def test_dry_run_writes_nothing(bf: ModuleType, tmp_path: Path) -> None:
    _touch(tmp_path, "drinko_Song.m4a")
    report = bf.backfill(tmp_path, [_clip(1, "Song")], dry_run=True)
    assert report["sidecars_written"] == 1 and report["dry_run"] is True
    assert [p.name for p in tmp_path.iterdir()] == ["drinko_Song.m4a"]


def test_fuzzy_pairs_only_unique_leftovers(bf: ModuleType, tmp_path: Path) -> None:
    _touch(tmp_path, "drinko_Cafe Noir.m4a")
    clips = [_clip(1, "Café Noir")]
    assert bf.backfill(tmp_path, clips, dry_run=True)["sidecars_written"] == 0
    report = bf.backfill(tmp_path, clips, fuzzy=True)
    assert report["sidecars_written"] == 1
    assert _sidecar(tmp_path, "drinko_Cafe Noir")["match"] == "normalised"


def test_cli_dry_run_and_unwritable_guard(
    bf: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arc = tmp_path / "arc"
    arc.mkdir()
    _touch(arc, "drinko_Song.m4a")
    lib = tmp_path / "library.json"
    lib.write_text(json.dumps([_clip(1, "Song"), _clip(2, "Hidden", is_public=False)]))
    assert bf.main([str(arc), str(lib), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would write: 1" in out and "private ignored: 1" in out
    assert list(arc.glob("*.json")) == []
    assert bf.main([str(arc), str(lib)]) == 0
    assert (arc / "drinko_Song.json").exists()
