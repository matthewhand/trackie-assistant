import json
import os
import subprocess
import sys

import pytest

from tests import windows_ledger_model as ledger

ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only prototype")


def write_owned(root, clip_id=ID1, stem="artist_title"):
    audio = f"{stem}.m4a"
    (root / audio).write_bytes(b"x" * 2048)
    (root / f"{stem}.json").write_text(json.dumps({"id": clip_id, "saved_files": [audio]}))


def sidecar_bytes(clip_id=ID1, stem="artist_title"):
    return json.dumps({"id": clip_id, "saved_files": [f"{stem}.m4a"]}).encode()


def complete_one(root):
    with ledger.bootstrap(root) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        store.publish(ID1, b"x" * 2048, sidecar_bytes())


@pytest.mark.parametrize(
    "name,is_dir",
    [
        ("old.m4a", False),
        ("old.json", False),
        (".legacy-intent", False),
        (".suno-rotation.json", False),
        (".library", True),
        (".suno-staging", True),
        (".trackie-identity-ledger", False),
        (".trackie-identity-lock", False),
    ],
)
def test_bootstrap_refuses_every_preexisting_archive_member(tmp_path, name, is_dir):
    path = tmp_path / name
    if is_dir:
        path.mkdir()
    else:
        path.write_bytes(b"legacy")
    with pytest.raises(ledger.LedgerError, match="pristine"):
        ledger.bootstrap(tmp_path)
    assert path.exists()


def test_bootstrap_reservation_completion_and_reopen(tmp_path):
    complete_one(tmp_path)
    with ledger.open_ledger(tmp_path) as store:
        store.reconcile()
        with pytest.raises(ledger.LedgerError, match="permanently reserved"):
            store.reserve(ID1, "different.m4a", "different.json")
        with pytest.raises(ledger.LedgerError, match="permanently reserved"):
            store.reserve(ID2, "ARTIST_TITLE.m4a", "ARTIST_TITLE.json")


def test_only_one_cooperating_writer_holds_the_lock(tmp_path):
    with ledger.bootstrap(tmp_path), pytest.raises(ledger.LedgerError, match="holds the lock"):
        ledger.open_ledger(tmp_path)
    with ledger.open_ledger(tmp_path):
        pass


def test_pending_reservation_persists_after_restart(tmp_path):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
    with ledger.open_ledger(tmp_path) as store:
        with pytest.raises(ledger.LedgerError, match="permanently reserved"):
            store.reserve(ID1, "different.m4a", "different.json")
        with pytest.raises(ledger.LedgerError, match="permanently reserved"):
            store.reserve(ID2, "ARTIST_TITLE.m4a", "ARTIST_TITLE.json")


@pytest.mark.parametrize("fraction", [0.1, 0.5, 0.99])
def test_torn_append_refuses_restart(tmp_path, monkeypatch, fraction):
    with ledger.bootstrap(tmp_path) as store:

        def interrupted(fd, payload):
            os.write(fd, payload[: max(1, int(len(payload) * fraction))])
            raise RuntimeError("simulated process interruption")

        monkeypatch.setattr(ledger, "_write_all", interrupted)
        with pytest.raises(RuntimeError):
            store.reserve(ID1, "artist_title.m4a", "artist_title.json")
    with pytest.raises(ledger.LedgerError, match="Truncated|Corrupt|canonical|hash"):
        ledger.open_ledger(tmp_path)


def test_uncertain_fsync_after_complete_append_does_not_free_reservation(tmp_path, monkeypatch):
    with ledger.bootstrap(tmp_path) as store:

        def interrupted(fd, payload):
            os.write(fd, payload)
            raise RuntimeError("before fsync")

        monkeypatch.setattr(ledger, "_write_all", interrupted)
        with pytest.raises(RuntimeError):
            store.reserve(ID1, "artist_title.m4a", "artist_title.json")
    with (
        ledger.open_ledger(tmp_path) as reopened,
        pytest.raises(ledger.LedgerError, match="permanently reserved"),
    ):
        reopened.reserve(ID1, "new.m4a", "new.json")


def test_interrupted_bootstrap_fails_closed(tmp_path, monkeypatch):
    def interrupted(fd, payload):
        os.write(fd, payload[:7])
        raise RuntimeError("power loss")

    monkeypatch.setattr(ledger, "_write_all", interrupted)
    with pytest.raises(RuntimeError):
        ledger.bootstrap(tmp_path)
    with pytest.raises(ledger.LedgerError):
        ledger.open_ledger(tmp_path)
    with pytest.raises(ledger.LedgerError, match="pristine"):
        ledger.bootstrap(tmp_path)


def test_corrupt_record_refuses_restart(tmp_path):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
    path = tmp_path / ledger._LEDGER
    data = path.read_bytes().replace(b"artist_title", b"artist_!itle", 1)
    path.write_bytes(data)
    with pytest.raises(ledger.LedgerError, match="hash"):
        ledger.open_ledger(tmp_path)


@pytest.mark.parametrize("lost", ["completion", "all_events"])
def test_lost_durable_suffix_with_surviving_audio_is_detected(tmp_path, lost):
    with ledger.bootstrap(tmp_path) as store:
        path = tmp_path / ledger._LEDGER
        header = path.read_bytes()
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        reserved = path.read_bytes()
        store.publish(ID1, b"x" * 2048, sidecar_bytes())
    path.write_bytes(reserved if lost == "completion" else header)
    with pytest.raises(ledger.LedgerError, match="Incomplete|Foreign"):
        ledger.open_ledger(tmp_path)


def test_missing_ledger_with_surviving_media_is_detected(tmp_path):
    complete_one(tmp_path)
    (tmp_path / ledger._LEDGER).unlink()
    with pytest.raises(ledger.LedgerError):
        ledger.open_ledger(tmp_path)
    with pytest.raises(ledger.LedgerError, match="pristine"):
        ledger.bootstrap(tmp_path)


@pytest.mark.parametrize("name", ["foreign.m4a", "foreign.json", ".old-intent", "notes.txt"])
def test_foreign_file_refuses_reconciliation(tmp_path, name):
    complete_one(tmp_path)
    (tmp_path / name).write_bytes(b"unowned")
    with pytest.raises(ledger.LedgerError, match="Foreign"):
        ledger.open_ledger(tmp_path)


@pytest.mark.parametrize("name", ["artist_title.m4a", "artist_title.json"])
def test_completed_file_changes_are_detected(tmp_path, name):
    complete_one(tmp_path)
    with (tmp_path / name).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ledger.LedgerError):
        ledger.open_ledger(tmp_path)


@pytest.mark.parametrize("bad", ["id", "saved_files"])
def test_incorrect_sidecar_cannot_publish(tmp_path, bad):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        data = {"id": ID1, "saved_files": ["artist_title.m4a"]}
        data[bad] = ID2 if bad == "id" else ["other.m4a"]
        with pytest.raises(ledger.LedgerError, match="Sidecar identity"):
            store.publish(ID1, b"x" * 2048, json.dumps(data).encode())
        assert not (tmp_path / "artist_title.m4a").exists()
    with ledger.open_ledger(tmp_path):
        pass


def test_hardlinks_are_refused(tmp_path):
    complete_one(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-hardlink")
    os.link(tmp_path / "artist_title.m4a", outside)
    with pytest.raises(ledger.LedgerError, match="hardlink"):
        ledger.open_ledger(tmp_path)


def test_symlink_is_refused(tmp_path):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        (tmp_path / "artist_title.m4a").symlink_to(tmp_path / "missing")
    with pytest.raises(ledger.LedgerError, match="Incomplete|Foreign"):
        ledger.open_ledger(tmp_path)


def test_lock_replacement_is_detected(tmp_path):
    with ledger.bootstrap(tmp_path) as store:
        (tmp_path / ledger._LOCK).unlink()
        (tmp_path / ledger._LOCK).write_text("replacement")
        with pytest.raises(ledger.LedgerError, match="lock was replaced"):
            store.reconcile()


def test_non_linux_host_is_explicitly_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.sys, "platform", "win32")
    with pytest.raises(ledger.LedgerError, match="Windows durability"):
        ledger.bootstrap(tmp_path)
    assert not list(tmp_path.iterdir())


def test_real_process_exit_during_append_fails_closed(tmp_path):
    with ledger.bootstrap(tmp_path):
        pass
    code = """
import os, sys
from pathlib import Path
from tests import windows_ledger_model as ledger
with ledger.open_ledger(Path(sys.argv[1])) as store:
    def interrupt(fd, payload):
        os.write(fd, payload[:len(payload)//2])
        os._exit(91)
    ledger._write_all = interrupt
    store.reserve(sys.argv[2], 'artist_title.m4a', 'artist_title.json')
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), ID1], env=env, check=False)
    assert result.returncode == 91
    with pytest.raises(ledger.LedgerError, match="Truncated|Corrupt|canonical|hash"):
        ledger.open_ledger(tmp_path)


def test_audio_fsync_failure_never_commits_completion(tmp_path, monkeypatch):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")

        def fail(_fd):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(ledger, "_sync_created", fail)
        with pytest.raises(OSError, match="fsync"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
    with pytest.raises(ledger.LedgerError, match="Foreign|Incomplete"):
        ledger.open_ledger(tmp_path)


def test_separate_process_cannot_plant_reserved_files_for_adoption(tmp_path):
    code = """
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / 'artist_title.m4a').write_bytes(b'foreign audio')
data = {'id': sys.argv[2], 'saved_files': ['artist_title.m4a']}
(root / 'artist_title.json').write_text(json.dumps(data))
"""
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path), ID1], check=False)
        assert result.returncode == 0
        before_audio = (tmp_path / "artist_title.m4a").read_bytes()
        before_sidecar = (tmp_path / "artist_title.json").read_bytes()
        with pytest.raises(ledger.LedgerError, match="Incomplete"):
            store.publish(ID1, b"new audio" * 300, sidecar_bytes())
        assert (tmp_path / "artist_title.m4a").read_bytes() == before_audio
        assert (tmp_path / "artist_title.json").read_bytes() == before_sidecar
    with pytest.raises(ledger.LedgerError, match="Incomplete|Foreign"):
        ledger.open_ledger(tmp_path)


@pytest.mark.parametrize(
    "stage",
    [
        "before_audio_create",
        "after_audio_create",
        "after_both_create",
        "during_audio_write",
        "after_audio_write",
        "during_sidecar_write",
        "audio_fsync",
        "sidecar_fsync",
        "directory_fsync",
        "before_completion_record",
        "during_completion_record",
        "after_stage_create",
        "before_audio_publish",
        "after_audio_publish",
        "before_sidecar_publish",
        "after_sidecar_publish",
    ],
)
def test_every_interrupted_publication_stage_preserves_reservation_and_fails_closed(
    tmp_path, monkeypatch, stage
):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        original_create = ledger._create_new
        original_write = ledger._write_created
        original_sync = ledger._sync_created
        original_stage = ledger._make_staging
        original_rename = ledger._rename_no_replace
        create_calls = 0
        write_calls = 0
        sync_calls = 0

        def create(path):
            nonlocal create_calls
            create_calls += 1
            if (stage == "before_audio_create" and create_calls == 1) or (
                stage == "after_audio_create" and create_calls == 2
            ):
                raise RuntimeError("injected interruption")
            return original_create(path)

        def write(fd, payload):
            nonlocal write_calls
            write_calls += 1
            if (stage == "after_both_create" and write_calls == 1) or (
                stage == "after_audio_write" and write_calls == 2
            ):
                raise RuntimeError("injected interruption")
            if (stage == "during_audio_write" and write_calls == 1) or (
                stage == "during_sidecar_write" and write_calls == 2
            ):
                os.write(fd, payload[: max(1, len(payload) // 2)])
                raise RuntimeError("injected interruption")
            original_write(fd, payload)

        def sync(fd):
            nonlocal sync_calls
            sync_calls += 1
            if (stage == "audio_fsync" and sync_calls == 1) or (
                stage == "sidecar_fsync" and sync_calls == 2
            ):
                raise RuntimeError("injected interruption")
            original_sync(fd)

        def fail(*_args, **_kwargs):
            raise RuntimeError("injected interruption")

        def torn_record(fd, payload):
            os.write(fd, payload[: len(payload) // 2])
            raise RuntimeError("injected interruption")

        def make_stage(root):
            value = original_stage(root)
            if stage == "after_stage_create":
                raise RuntimeError("injected interruption")
            return value

        def rename(source, destination):
            target = "audio" if destination.suffix == ".m4a" else "sidecar"
            if stage == f"before_{target}_publish":
                raise RuntimeError("injected interruption")
            original_rename(source, destination)
            if stage == f"after_{target}_publish":
                raise RuntimeError("injected interruption")

        monkeypatch.setattr(ledger, "_make_staging", make_stage)
        monkeypatch.setattr(ledger, "_rename_no_replace", rename)
        monkeypatch.setattr(ledger, "_create_new", create)
        monkeypatch.setattr(ledger, "_write_created", write)
        monkeypatch.setattr(ledger, "_sync_created", sync)
        if stage == "directory_fsync":
            monkeypatch.setattr(ledger, "_sync_dir", fail)
        elif stage == "before_completion_record":
            monkeypatch.setattr(store, "_append", fail)
        elif stage == "during_completion_record":
            monkeypatch.setattr(ledger, "_write_all", torn_record)
        with pytest.raises(RuntimeError, match="injected interruption"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
    with pytest.raises(ledger.LedgerError):
        ledger.open_ledger(tmp_path)


@pytest.mark.parametrize("planted", ["audio", "sidecar"])
def test_exclusive_rename_refuses_destination_planted_after_reconciliation(
    tmp_path, monkeypatch, planted
):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        original = ledger._rename_no_replace
        target = tmp_path / ("artist_title.m4a" if planted == "audio" else "artist_title.json")

        def race(source, destination):
            if destination == target:
                destination.write_bytes(b"foreign data")
            original(source, destination)

        monkeypatch.setattr(ledger, "_rename_no_replace", race)
        with pytest.raises(ledger.LedgerError, match="no-replace"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
        assert target.read_bytes() == b"foreign data"
    with pytest.raises(ledger.LedgerError):
        ledger.open_ledger(tmp_path)


def test_replaced_created_inode_is_detected_before_completion(tmp_path, monkeypatch):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        original = ledger._rename_no_replace
        displaced = tmp_path.parent / (tmp_path.name + "-displaced-audio")

        def replace_after_rename(source, destination):
            original(source, destination)
            if destination.name == "artist_title.m4a":
                destination.rename(displaced)
                destination.write_bytes(b"foreign data")

        monkeypatch.setattr(ledger, "_rename_no_replace", replace_after_rename)
        with pytest.raises(ledger.LedgerError, match="replaced"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
        assert displaced.read_bytes() == b"x" * 2048
        assert (tmp_path / "artist_title.m4a").read_bytes() == b"foreign data"
    with pytest.raises(ledger.LedgerError, match="Incomplete|Foreign"):
        ledger.open_ledger(tmp_path)


def test_durable_completion_remains_valid_if_publisher_exits_after_fsync(tmp_path, monkeypatch):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        original = ledger._write_all

        def interrupted_after_sync(fd, payload):
            original(fd, payload)
            raise RuntimeError("publisher exited after durable completion")

        monkeypatch.setattr(ledger, "_write_all", interrupted_after_sync)
        with pytest.raises(RuntimeError, match="durable completion"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
    with ledger.open_ledger(tmp_path) as reopened:
        reopened.reconcile()


@pytest.mark.parametrize(
    "stage",
    [
        "after_audio_create",
        "after_audio_write",
        "after_sidecar_write",
        "before_record",
        "before_audio_publish",
        "after_audio_publish",
        "before_sidecar_publish",
        "after_sidecar_publish",
    ],
)
def test_real_process_exit_during_publication_refuses_restart(tmp_path, stage):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
    code = """
import json, os, sys
from pathlib import Path
from tests import windows_ledger_model as ledger
stage=sys.argv[3]
with ledger.open_ledger(Path(sys.argv[1])) as store:
    create=ledger._create_new
    write=ledger._write_created
    rename=ledger._rename_no_replace
    counts={'create':0, 'write':0}
    def new(path):
        fd=create(path)
        counts['create']+=1
        if stage=='after_audio_create' and counts['create']==1:
            os._exit(92)
        return fd
    def put(fd, data):
        write(fd, data)
        counts['write']+=1
        if stage=='after_audio_write' and counts['write']==1:
            os._exit(92)
        if stage=='after_sidecar_write' and counts['write']==2:
            os._exit(92)
    def move(source, destination):
        kind='audio' if destination.suffix=='.m4a' else 'sidecar'
        if stage=='before_'+kind+'_publish':
            os._exit(92)
        rename(source, destination)
        if stage=='after_'+kind+'_publish':
            os._exit(92)
    ledger._create_new=new
    ledger._write_created=put
    ledger._rename_no_replace=move
    if stage=='before_record':
        store._append=lambda *args: os._exit(92)
    sidecar=json.dumps({'id':sys.argv[2], 'saved_files':['artist_title.m4a']}).encode()
    store.publish(sys.argv[2], b'x'*2048, sidecar)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), ID1, stage],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 92
    with pytest.raises(ledger.LedgerError, match="Incomplete|Foreign"):
        ledger.open_ledger(tmp_path)


def test_readers_never_see_partial_final_media_or_sidecar(tmp_path, monkeypatch):
    final_audio = tmp_path / "artist_title.m4a"
    final_sidecar = tmp_path / "artist_title.json"
    audio = b"sample-data" * 300
    sidecar = sidecar_bytes()
    observations = []
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, final_audio.name, final_sidecar.name)
        original_write = ledger._write_created
        original_rename = ledger._rename_no_replace

        def observe_write(fd, payload):
            split = len(payload) // 2
            os.write(fd, payload[:split])
            observations.append((final_audio.exists(), final_sidecar.exists()))
            assert not final_audio.exists() and not final_sidecar.exists()
            original_write(fd, payload[split:])

        def observe_rename(source, destination):
            if destination == final_audio:
                assert not final_audio.exists() and not final_sidecar.exists()
            else:
                assert final_audio.read_bytes() == audio and not final_sidecar.exists()
            original_rename(source, destination)
            if destination == final_audio:
                assert final_audio.read_bytes() == audio
            else:
                assert final_sidecar.read_bytes() == sidecar

        monkeypatch.setattr(ledger, "_write_created", observe_write)
        monkeypatch.setattr(ledger, "_rename_no_replace", observe_rename)
        store.publish(ID1, audio, sidecar)
    assert observations == [(False, False), (False, False)]
    assert final_audio.read_bytes() == audio and final_sidecar.read_bytes() == sidecar


def test_hardlinked_staging_file_is_refused_without_publishing(tmp_path, monkeypatch):
    with ledger.bootstrap(tmp_path) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json")
        original = ledger._write_created
        outside = tmp_path.parent / (tmp_path.name + "-staging-hardlink")
        calls = 0

        def hardlink_staged(fd, data):
            nonlocal calls
            original(fd, data)
            calls += 1
            if calls == 1:
                stage = next(tmp_path.glob(".trackie-publish-*"))
                os.link(stage / "audio.part", outside)

        monkeypatch.setattr(ledger, "_write_created", hardlink_staged)
        with pytest.raises(ledger.LedgerError, match="hardlink"):
            store.publish(ID1, b"x" * 2048, sidecar_bytes())
        assert not (tmp_path / "artist_title.m4a").exists()
        assert outside.read_bytes() == b"x" * 2048
    with pytest.raises(ledger.LedgerError, match="Foreign"):
        ledger.open_ledger(tmp_path)


def test_abandoned_staging_is_not_adopted_or_deleted_on_restart(tmp_path):
    with ledger.bootstrap(tmp_path):
        pass
    stage = tmp_path / ".trackie-publish-abandoned"
    stage.mkdir()
    evidence = stage / "audio.part"
    evidence.write_bytes(b"do not delete")
    with pytest.raises(ledger.LedgerError, match="Foreign"):
        ledger.open_ledger(tmp_path)
    assert evidence.read_bytes() == b"do not delete"
