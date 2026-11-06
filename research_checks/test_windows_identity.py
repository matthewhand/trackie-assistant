import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid

import pytest

from tests import windows_identity_model as identity

ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only candidate")


def recording(root, clip_id=ID1, stem="artist_title"):
    root.mkdir(parents=True, exist_ok=True)
    audio = root / f"{stem}.m4a"
    audio.write_bytes(b"x" * 2048)
    sidecar = root / f"{stem}.json"
    sidecar.write_text(json.dumps({"id": clip_id, "saved_files": [audio.name]}))
    return audio, sidecar


def test_explicit_empty_initialization_and_reopen_enforce_sqlite_settings(tmp_path):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "persist"
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 3
    for _ in range(2):
        with identity.open_ledger(
            tmp_path, archive_lock_held=True, storage_verified_local=True
        ) as store:
            assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "persist"
            assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 3


def test_lock_assertion_required_and_no_implicit_bootstrap(tmp_path):
    with pytest.raises(identity.IdentityLedgerError, match="archive lock"):
        identity.initialize(tmp_path)
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert not list(tmp_path.iterdir())


def test_existing_import_is_explicit_and_preserves_files(tmp_path):
    audio, sidecar = recording(tmp_path / "playlist")
    before = {p: p.read_bytes() for p in (audio, sidecar)}
    with pytest.raises(identity.IdentityLedgerError, match="explicit opt-in"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert not (tmp_path / identity._DB).exists()
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ) as store:
        store.reconcile(archive_lock_held=True)
    with identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    assert {p: p.read_bytes() for p in (audio, sidecar)} == before


def test_reservations_survive_reopen_and_block_reuse(tmp_path):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        with pytest.raises(identity.IdentityLedgerError, match="reserved"):
            store.reserve(ID1, "other.m4a", "other.json", archive_lock_held=True)
        with pytest.raises(identity.IdentityLedgerError, match="reserved"):
            store.reserve(ID2, "ARTIST.m4a", "ARTIST.json", archive_lock_held=True)


@pytest.mark.parametrize("mode", ["audio", "sidecar", "both"])
def test_incomplete_reserved_output_refuses_restart(tmp_path, mode):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    if mode in {"audio", "both"}:
        (tmp_path / "artist.m4a").write_bytes(b"x" * 2048)
    if mode in {"sidecar", "both"}:
        (tmp_path / "artist.json").write_text(
            json.dumps({"id": ID1, "saved_files": ["artist.m4a"]})
        )
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize("foreign", ["foreign.m4a", "foreign.json", "notes.txt", ".legacy-intent"])
def test_unindexed_files_refuse_open(tmp_path, foreign):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    (tmp_path / foreign).write_bytes(b"unindexed")
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize("member", ["audio", "sidecar"])
def test_changed_imported_media_or_sidecar_is_detected(tmp_path, member):
    audio, sidecar = recording(tmp_path)
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ):
        pass
    path = audio if member == "audio" else sidecar
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_missing_database_cannot_silently_reinitialize(tmp_path):
    recording(tmp_path)
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ):
        pass
    (tmp_path / identity._DB).unlink()
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)
    with pytest.raises(identity.IdentityLedgerError, match="ambiguous"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )


def test_stale_database_with_new_media_is_detected(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    old = tmp_path.parent / (tmp_path.name + "-old.sqlite3")
    shutil.copy2(tmp_path / identity._DB, old)
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json", archive_lock_held=True)
    shutil.copy2(old, tmp_path / identity._DB)
    recording(tmp_path)
    with pytest.raises(identity.IdentityLedgerError, match="Unindexed|stale"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_corrupt_database_fails_closed(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    (tmp_path / identity._DB).write_bytes(b"not a database")
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize(
    "name,is_dir",
    [
        (".suno-staging", True),
        (".library", True),
        (".intent.json", False),
        (".trackie-identity.sqlite3-journal", False),
    ],
)
def test_import_refuses_legacy_or_ambiguous_state(tmp_path, name, is_dir):
    path = tmp_path / name
    if is_dir:
        path.mkdir()
    else:
        path.write_bytes(b"legacy")
    with pytest.raises(identity.IdentityLedgerError):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )
    assert path.exists() and not (tmp_path / identity._MARKER).exists()


@pytest.mark.parametrize("kind", ["audio", "sidecar", "directory"])
def test_symlinks_are_refused(tmp_path, kind):
    if kind == "directory":
        target = tmp_path.parent / (tmp_path.name + "-target")
        target.mkdir()
        (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    else:
        audio, sidecar = recording(tmp_path)
        path = audio if kind == "audio" else sidecar
        path.unlink()
        path.symlink_to(tmp_path / "missing")
    with pytest.raises(identity.IdentityLedgerError, match="Symlink|reparse"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )


def test_hardlinked_media_is_refused(tmp_path):
    audio, _ = recording(tmp_path)
    os.link(audio, tmp_path.parent / (tmp_path.name + "-hardlink"))
    with pytest.raises(identity.IdentityLedgerError, match="hardlink"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )


def test_network_filesystem_is_refused_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "_filesystem_type", lambda _path: "nfs4")
    with pytest.raises(identity.IdentityLedgerError, match="network filesystem"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert not list(tmp_path.iterdir())


def test_reparse_point_is_refused_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "_is_reparse", lambda _info: True)
    with pytest.raises(identity.IdentityLedgerError, match="Unsafe archive root"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert not list(tmp_path.iterdir())


def test_wal_auxiliary_file_is_refused(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    (tmp_path / (identity._DB + "-wal")).write_bytes(b"ambiguous")
    with pytest.raises(identity.IdentityLedgerError, match="WAL"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_changed_durability_setting_fails_closed(tmp_path):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store._conn.execute("PRAGMA synchronous=NORMAL")
        with pytest.raises(identity.IdentityLedgerError, match="durability settings"):
            store.reconcile(archive_lock_held=True)


def test_committed_reservation_survives_real_process_exit(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    code = """
import os, sys
from pathlib import Path
from tests.windows_identity_model import open_ledger
with open_ledger(Path(sys.argv[1]), archive_lock_held=True, storage_verified_local=True) as store:
    store.reserve(sys.argv[2], 'artist.m4a', 'artist.json', archive_lock_held=True)
    os._exit(91)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), ID1],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 91
    with (
        identity.open_ledger(
            tmp_path, archive_lock_held=True, storage_verified_local=True
        ) as store,
        pytest.raises(identity.IdentityLedgerError, match="reserved"),
    ):
        store.reserve(ID1, "other.m4a", "other.json", archive_lock_held=True)


def test_uncommitted_witnessed_sqlite_transaction_requires_review(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    code = """
import os, sys, uuid
from pathlib import Path
from tests import windows_identity_model as m
with m.open_ledger(Path(sys.argv[1]), archive_lock_held=True, storage_verified_local=True) as store:
    root=Path(sys.argv[1]); marker=m._marker(root)
    m._witness_append(root, marker, 'dirty', uuid.uuid4().hex)
    conn=store._conn
    conn.execute('BEGIN IMMEDIATE')
    conn.execute("INSERT INTO identities VALUES (?,?,?,?,?,'reserved',NULL,NULL)",
                 (sys.argv[2], 'artist.m4a', 'artist.json', 'artist.m4a', 'artist.json'))
    os._exit(92)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), ID1],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 92
    with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_local_storage_must_be_explicitly_attested(tmp_path):
    with pytest.raises(identity.IdentityLedgerError, match="backing storage is local"):
        identity.initialize(tmp_path, archive_lock_held=True)
    assert not list(tmp_path.iterdir())


def test_non_persist_sqlite_connection_is_rejected():
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(identity.IdentityLedgerError, match="durability settings"):
            identity._configure(connection)
    finally:
        connection.close()


def test_archive_lock_assertion_required_on_every_operation(tmp_path):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        with pytest.raises(identity.IdentityLedgerError, match="archive lock"):
            store.reconcile()
        with pytest.raises(identity.IdentityLedgerError, match="archive lock"):
            store.reserve(ID1, "artist.m4a", "artist.json")


def test_interrupted_initialization_does_not_allow_silent_reinitialization(tmp_path, monkeypatch):
    def interrupted(_root):
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(identity, "_sync_dir", interrupted)
    with pytest.raises(OSError, match="fsync"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert (tmp_path / identity._MARKER).exists()
    with pytest.raises(identity.IdentityLedgerError, match="ambiguous"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_hardlinked_database_is_rejected_before_open(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    os.link(tmp_path / identity._DB, tmp_path.parent / (tmp_path.name + "-db-hardlink"))
    with pytest.raises(identity.IdentityLedgerError, match="hardlink"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_unexpected_database_schema_objects_are_rejected(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    conn = sqlite3.connect(tmp_path / identity._DB)
    try:
        conn.execute("CREATE TABLE foreign_table (value TEXT)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(identity.IdentityLedgerError, match="schema objects"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_network_submount_is_rejected_during_import(tmp_path, monkeypatch):
    recording(tmp_path / "nested")
    monkeypatch.setattr(
        identity, "_filesystem_type", lambda path: "nfs4" if path.name == "nested" else "ext4"
    )
    with pytest.raises(identity.IdentityLedgerError, match="network filesystem"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )
    assert not (tmp_path / identity._MARKER).exists()


def test_stale_database_alone_is_detected_even_without_output(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    stale = tmp_path.parent / (tmp_path.name + "-stale-db")
    shutil.copy2(tmp_path / identity._DB, stale)
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    shutil.copy2(stale, tmp_path / identity._DB)
    with pytest.raises(identity.IdentityLedgerError, match="stale"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_coordinated_database_and_witness_rollback_without_output_is_undetectable(tmp_path):
    # Characterizes the explicitly unsupported absence of an external witness.
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    old_db = (tmp_path / identity._DB).read_bytes()
    old_witness = (tmp_path / identity._WITNESS).read_bytes()
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    (tmp_path / identity._DB).write_bytes(old_db)
    (tmp_path / identity._WITNESS).write_bytes(old_witness)
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        # NOT safe production behavior; there is no external monotonic anchor.
        store.reserve(ID1, "different.m4a", "different.json", archive_lock_held=True)


def test_characterizes_missing_rollback_journal_cannot_be_distinguished(tmp_path):
    # Clean journal absence is accepted by SQLite. The model cannot tell it
    # apart from a missing hot journal; this must not be used as crash proof.
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    journal = tmp_path / identity._JOURNAL
    assert journal.exists()
    journal.unlink()
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reconcile(archive_lock_held=True)


def grouped_recording(root, clip_id=ID1, stem="artist_title"):
    root.mkdir(parents=True, exist_ok=True)
    members = {
        f"{stem}.m4a": b"audio" * 500,
        f"{stem}.mp4": b"video" * 500,
        f"{stem}.cover.jpeg": b"\xff\xd8image\xff\xd9",
    }
    for name, content in members.items():
        (root / name).write_bytes(content)
    sidecar = root / f"{stem}.json"
    sidecar.write_text(json.dumps({"id": clip_id, "saved_files": sorted(members)}))
    members[sidecar.name] = sidecar.read_bytes()
    return members


def test_multi_media_and_listed_cover_import_reconciles_every_member(tmp_path):
    before = grouped_recording(tmp_path)
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ) as store:
        rows = store._conn.execute("SELECT path,role FROM members ORDER BY path").fetchall()
        assert rows == [
            ("artist_title.cover.jpeg", "cover"),
            ("artist_title.m4a", "media"),
            ("artist_title.mp4", "media"),
        ]
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reconcile(archive_lock_held=True)
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


def test_populated_import_requires_ownership_review_assertion(tmp_path):
    before = grouped_recording(tmp_path)
    with pytest.raises(identity.IdentityLedgerError, match="review of member ownership"):
        identity.initialize(
            tmp_path, import_existing=True, archive_lock_held=True, storage_verified_local=True
        )
    assert not (tmp_path / identity._MARKER).exists()
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


@pytest.mark.parametrize(
    "name", ["artist_title.m4a", "artist_title.mp4", "artist_title.cover.jpeg", "artist_title.json"]
)
def test_change_or_loss_of_any_group_member_is_detected(tmp_path, name):
    grouped_recording(tmp_path)
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ):
        pass
    (tmp_path / name).unlink()
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_unlisted_cover_is_rejected_before_import(tmp_path):
    recording(tmp_path)
    cover = tmp_path / "artist_title.cover.jpeg"
    cover.write_bytes(b"foreign cover")
    with pytest.raises(identity.IdentityLedgerError, match="Unindexed"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )
    assert cover.read_bytes() == b"foreign cover"
    assert not (tmp_path / identity._MARKER).exists()


def test_casefold_collision_across_groups_is_rejected(tmp_path):
    grouped_recording(tmp_path, ID1, "Artist")
    grouped_recording(tmp_path, ID2, "artist")
    with pytest.raises(identity.IdentityLedgerError, match="Conflicting|claimed"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )
    assert not (tmp_path / identity._MARKER).exists()


def test_reservation_cannot_reuse_imported_nonprimary_member(tmp_path):
    grouped_recording(tmp_path)
    with (
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        ) as store,
        pytest.raises(identity.IdentityLedgerError),
    ):
        store.reserve(ID2, "artist_title.mp4", "artist_title.json", archive_lock_held=True)


def test_missing_member_database_row_is_detected(tmp_path):
    grouped_recording(tmp_path)
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ) as store:
        store._conn.execute("DELETE FROM members WHERE path=?", ("artist_title.cover.jpeg",))
        with pytest.raises(identity.IdentityLedgerError, match="Unindexed|stale"):
            store.reconcile(archive_lock_held=True)


def test_realistic_333_cover_and_30_media_only_groups_roundtrip(tmp_path):
    # Synthetic contents: this validates identity accounting, not media decoding.
    for i in range(363):
        clip_id = f"{i + 1:08x}-1111-4111-8111-111111111111"
        stem = f"track_{i:03}"
        (tmp_path / f"{stem}.m4a").write_bytes(b"x" * 2048)
        saved = [f"{stem}.m4a"]
        if i < 333:
            (tmp_path / f"{stem}.cover.jpeg").write_bytes(b"\xff\xd8cover\xff\xd9")
            saved.append(f"{stem}.cover.jpeg")
        (tmp_path / f"{stem}.json").write_text(json.dumps({"id": clip_id, "saved_files": saved}))
    with identity.initialize(
        tmp_path,
        import_existing=True,
        ownership_reviewed=True,
        archive_lock_held=True,
        storage_verified_local=True,
    ) as store:
        assert store._conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 363
        assert store._conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 696
    with identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass


def test_process_exit_during_group_import_keeps_original_members_and_refuses_open(tmp_path):
    before = grouped_recording(tmp_path)
    code = """
import os, sys
from pathlib import Path
from tests import windows_identity_model as m
original=m._scan
count=0
def scan(root, *, initialized):
    global count
    count+=1
    if initialized:
        os._exit(93)
    return original(root, initialized=initialized)
m._scan=scan
m.initialize(Path(sys.argv[1]), import_existing=True, ownership_reviewed=True,
             archive_lock_held=True, storage_verified_local=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 93
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)
    with pytest.raises(identity.IdentityLedgerError, match="ambiguous"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )


def test_mounted_ledger_file_is_rejected_before_sqlite_open(tmp_path, monkeypatch):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    before = (tmp_path / identity._DB).read_bytes()
    original = identity._filesystem_type

    def filesystem(path):
        return "nfs4" if path.name == identity._DB else original(path)

    monkeypatch.setattr(identity, "_filesystem_type", filesystem)
    with pytest.raises(identity.IdentityLedgerError, match="network filesystem"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)
    assert (tmp_path / identity._DB).read_bytes() == before


def test_uppercase_sidecar_extension_refused_before_initialization(tmp_path):
    audio, sidecar = recording(tmp_path)
    uppercase = sidecar.with_suffix(".JSON")
    sidecar.rename(uppercase)
    before = {audio: audio.read_bytes(), uppercase: uppercase.read_bytes()}
    with pytest.raises(identity.IdentityLedgerError, match="lowercase"):
        identity.initialize(
            tmp_path,
            import_existing=True,
            ownership_reviewed=True,
            archive_lock_held=True,
            storage_verified_local=True,
        )
    assert not (tmp_path / identity._MARKER).exists()
    assert not (tmp_path / identity._DB).exists()
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("remove_journal", [False, True])
def test_dirty_witness_is_refused_before_sqlite_open(tmp_path, monkeypatch, remove_journal):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    marker = identity._marker(tmp_path)
    identity._witness_append(tmp_path, marker, "dirty", uuid.uuid4().hex)
    if remove_journal:
        (tmp_path / identity._JOURNAL).unlink(missing_ok=True)

    def forbidden(_root):
        raise AssertionError("SQLite must not open before DIRTY is rejected")

    monkeypatch.setattr(identity, "_connect", forbidden)
    with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_missing_witness_refused_before_sqlite_open(tmp_path, monkeypatch):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    (tmp_path / identity._WITNESS).unlink()

    def forbidden(_root):
        raise AssertionError("SQLite must not open without the witness")

    monkeypatch.setattr(identity, "_connect", forbidden)
    with pytest.raises(identity.IdentityLedgerError):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)
    with pytest.raises(identity.IdentityLedgerError, match="ambiguous"):
        identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_truncated_witness_refused_before_sqlite_open(tmp_path, monkeypatch):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    with (tmp_path / identity._WITNESS).open("ab") as stream:
        stream.write(b'{"seq":')

    def forbidden(_root):
        raise AssertionError("SQLite must not open with a truncated witness")

    monkeypatch.setattr(identity, "_connect", forbidden)
    with pytest.raises(identity.IdentityLedgerError, match="truncated"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_stale_witness_alone_does_not_authorize_old_state(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    old_witness = (tmp_path / identity._WITNESS).read_bytes()
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    (tmp_path / identity._WITNESS).write_bytes(old_witness)
    with pytest.raises(identity.IdentityLedgerError, match="stale"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_torn_dirty_witness_write_refuses_restart(tmp_path, monkeypatch):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:

        def interrupted(fd, data):
            os.write(fd, data[:13])
            os.fsync(fd)
            raise OSError("witness fsync interruption")

        monkeypatch.setattr(identity, "_witness_write_all", interrupted)
        with pytest.raises(OSError, match="witness"):
            store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
    with pytest.raises(identity.IdentityLedgerError, match="truncated"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize(
    "phase",
    [
        "reserve_dirty",
        "reserve_begin",
        "reserve_before_commit",
        "reserve_after_commit",
        "reserve_clean",
    ],
)
def test_real_process_exit_at_each_reservation_witness_boundary(tmp_path, phase):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    code = """
import os, sys
from pathlib import Path
from tests import windows_identity_model as m
with m.open_ledger(Path(sys.argv[1]), archive_lock_held=True, storage_verified_local=True) as store:
    def fault(name):
        if name == sys.argv[3]: os._exit(94)
    m._fault_point=fault
    store.reserve(sys.argv[2], 'artist.m4a', 'artist.json', archive_lock_held=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), ID1, phase],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 94
    if phase == "reserve_clean":
        with (
            identity.open_ledger(
                tmp_path, archive_lock_held=True, storage_verified_local=True
            ) as store,
            pytest.raises(identity.IdentityLedgerError, match="reserved"),
        ):
            store.reserve(ID1, "other.m4a", "other.json", archive_lock_held=True)
    else:
        with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
            identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize(
    "phase", ["open_dirty", "open_before_commit", "open_after_commit", "open_clean"]
)
def test_real_process_exit_at_each_open_witness_boundary(tmp_path, phase):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    code = """
import os, sys
from pathlib import Path
from tests import windows_identity_model as m
def fault(name):
    if name == sys.argv[2]: os._exit(95)
m._fault_point=fault
m.open_ledger(Path(sys.argv[1]), archive_lock_held=True, storage_verified_local=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), phase],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 95
    if phase == "open_clean":
        with identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True):
            pass
    else:
        with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
            identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


@pytest.mark.parametrize(
    "phase", ["init_dirty", "init_before_commit", "init_after_commit", "init_clean"]
)
def test_real_process_exit_at_each_import_witness_boundary(tmp_path, phase):
    before = grouped_recording(tmp_path)
    code = """
import os, sys
from pathlib import Path
from tests import windows_identity_model as m
def fault(name):
    if name == sys.argv[2]: os._exit(96)
m._fault_point=fault
m.initialize(Path(sys.argv[1]), import_existing=True, ownership_reviewed=True,
             archive_lock_held=True, storage_verified_local=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), phase],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
    )
    assert result.returncode == 96
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    if phase == "init_clean":
        with identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True):
            pass
    else:
        with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
            identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_coordinated_old_state_with_surviving_group_is_rejected(tmp_path):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    old_db = (tmp_path / identity._DB).read_bytes()
    old_witness = (tmp_path / identity._WITNESS).read_bytes()
    with identity.open_ledger(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:
        store.reserve(ID1, "artist_title.m4a", "artist_title.json", archive_lock_held=True)
    recording(tmp_path)
    (tmp_path / identity._DB).write_bytes(old_db)
    (tmp_path / identity._WITNESS).write_bytes(old_witness)
    with pytest.raises(identity.IdentityLedgerError, match="Unindexed|stale"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_corrupted_witness_is_rejected_before_sqlite_open(tmp_path, monkeypatch):
    with identity.initialize(tmp_path, archive_lock_held=True, storage_verified_local=True):
        pass
    path = tmp_path / identity._WITNESS
    contents = path.read_bytes()
    path.write_bytes(contents.replace(b'"state":"dirty"', b'"state":"d1rty"', 1))

    def forbidden(_root):
        raise AssertionError("SQLite must not open when witness integrity fails")

    monkeypatch.setattr(identity, "_connect", forbidden)
    with pytest.raises(identity.IdentityLedgerError, match="integrity"):
        identity.open_ledger(tmp_path, archive_lock_held=True, storage_verified_local=True)


def test_caught_reservation_failure_checks_dirty_witness_before_reusing_connection(
    tmp_path, monkeypatch
):
    with identity.initialize(
        tmp_path, archive_lock_held=True, storage_verified_local=True
    ) as store:

        def interrupt(name):
            if name == "reserve_before_commit":
                raise RuntimeError("injected transaction failure")

        monkeypatch.setattr(identity, "_fault_point", interrupt)
        with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
            store.reserve(ID1, "artist.m4a", "artist.json", archive_lock_held=True)
        original = store._conn
        calls = []

        class NoSQLite:
            def execute(self, *args, **kwargs):
                calls.append(args)
                raise AssertionError("SQLite was accessed before checking DIRTY")

            def close(self):
                original.close()

        store._conn = NoSQLite()
        with pytest.raises(identity.IdentityLedgerError, match="DIRTY"):
            store.reconcile(archive_lock_held=True)
        assert calls == []
