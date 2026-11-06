"""Real process death tests for the production Linux migration journal.

These establish process interruption behavior, not hardware power-loss claims.
No production files or network sources are accessed.
"""

import queue
import subprocess
import sys
import threading

import pytest

from suno_dl import producer_migration as migration
from tests.test_producer_migration import _m4a_with_opaque_locale

CHILD = r"""
import sys,time,json,hashlib
from pathlib import Path
from suno_dl import producer_migration as m
root=Path(sys.argv[1]);phase=sys.argv[2]
mapping={"11111111-1111-4111-8111-111111111111":"Producer"}
def ready():
 print("READY",flush=True);time.sleep(300)
rename=m._rename_no_replace;save=m._save_journal;replace=m.os.replace;complete=m._publish_completion
def renamed(old,new):
 rename(old,new)
 if phase=="after_rename" and Path(new).suffix==".m4a":ready()
 if phase=="after_sidecar" and Path(new).name=="Producer_A song.json":ready()
def saved(journal,plan):
 prepared=any(t.get("prepared") for g in plan["groups"] for t in g["tags"])
 if phase=="unrecorded_stage" and prepared:ready()
 save(journal,plan)
 if phase=="after_prepared" and prepared:ready()
def replaced(old,new):
 replace(old,new)
 if phase=="after_audio_replace" and Path(new).name=="Producer_A song.m4a":ready()
def completed(*args):
 complete(*args)
 if phase=="after_completion":ready()
m._rename_no_replace=renamed;m._save_journal=saved;m.os.replace=replaced;m._publish_completion=completed
plan=m.plan_migration(root,mapping)
m.apply_migration(root,mapping,writers_stopped=True,producer_mapping_reviewed=True,
 reviewed_producers=mapping,reviewed_plan_sha256=m.review_plan_digest(plan))
"""

RESUME = r"""
import sys,hashlib
from pathlib import Path
from suno_dl import producer_migration as m
root=Path(sys.argv[1])
print(m.resume_migration(root,writers_stopped=True,producer_mapping_reviewed=True,
 reviewed_journal_sha256=hashlib.sha256(m.journal_path(root).read_bytes()).hexdigest()))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux migration")
@pytest.mark.parametrize(
    "phase",
    [
        "after_rename",
        "after_prepared",
        "after_audio_replace",
        "after_sidecar",
        "after_completion",
        "unrecorded_stage",
    ],
)
def test_process_kill_then_fresh_process_recovery(tmp_path, phase):
    source = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    before = source.read_bytes()
    signature = migration._audio_signature(source)
    pos = before.index(b"zzzz") - 4
    size = int.from_bytes(before[pos : pos + 4], "big")
    opaque = before[pos : pos + size]
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path), phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = queue.Queue()
    thread = threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True)
    thread.start()
    try:
        line = output.get(timeout=30)
        assert line.strip() == "READY", line + child.stdout.read()
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()
    journal = migration.journal_path(tmp_path)
    assert journal.exists()
    snapshot = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    resumed = subprocess.run(
        [sys.executable, "-c", RESUME, str(tmp_path)], capture_output=True, text=True, timeout=30
    )
    if phase == "unrecorded_stage":
        assert resumed.returncode != 0 and "Unjournalled audio stage" in resumed.stderr
        assert snapshot == {
            p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        return
    assert resumed.returncode == 0, resumed.stderr
    target = tmp_path / "Producer_A song.m4a"
    assert target.exists() and not source.exists() and not journal.exists()
    assert len(list(tmp_path.glob("*.m4a"))) == 1
    assert migration._read_artist(target) == "Producer"
    assert migration._audio_signature(target) == signature
    assert opaque in target.read_bytes()
