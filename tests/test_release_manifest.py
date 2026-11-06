"""Release-manifest tests (#163): pin a wheel to a commit, reproducibly."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_manifest.py"


def _module():
    spec = importlib.util.spec_from_file_location("release_manifest", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rm = _module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _synthetic_repo(tmp_path: Path) -> Path:
    """A tiny but real package, so builds exercise setuptools, not a mock."""
    repo = tmp_path / "repo"
    (repo / "src" / "suno_dl").mkdir(parents=True)
    # Mirror the real repository: package bytes must be LF on every export.
    (repo / ".gitattributes").write_bytes(b"src/suno_dl/** text=auto eol=lf\n")
    (repo / "src" / "suno_dl" / "__init__.py").write_bytes(b'__version__ = "0.0.1"\n')
    (repo / "src" / "suno_dl" / "data").mkdir()
    (repo / "src" / "suno_dl" / "data" / "catalogue.json").write_text('{"ok": true}\n')
    (repo / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "synthetic-release"',
                'version = "0.0.1"',
                'description = "synthetic"',
                'requires-python = ">=3.13"',
                "",
                "[tool.setuptools.packages.find]",
                'where = ["src"]',
                "",
                "[tool.setuptools.package-data]",
                'suno_dl = ["data/*.json"]',
                "",
            ]
        )
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release@example.invalid")
    _git(repo, "config", "user.name", "release test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "synthetic release")
    return repo


@pytest.mark.skipif(sys.version_info < (3, 13), reason="project requires Python 3.13")
def test_manifest_is_reproducible_and_matches_head() -> None:
    """Two builds of one commit must produce the same wheel hash — the defect
    that made a wheel hash useless as a provenance anchor."""
    first = rm.build_manifest(ROOT, "HEAD")
    second = rm.build_manifest(ROOT, "HEAD")

    head = _git(ROOT, "rev-parse", "HEAD")
    assert first["commit"]["sha"] == head
    assert first["commit"]["committed_epoch"] == int(_git(ROOT, "log", "-1", "--format=%ct", head))
    assert first["wheel"]["sha256"] == second["wheel"]["sha256"]
    assert first["wheel"]["members"] == second["wheel"]["members"]

    assert first["package"]["name"] == "trackie-assistant"
    assert first["package"]["requires_python"] == ">=3.13"
    assert (
        first["package"]["version"]
        == (ROOT / "pyproject.toml").read_text().split('version = "')[1].split('"')[0]
    )


def test_manifest_ships_exactly_the_committed_package_files() -> None:
    manifest = rm.build_manifest(ROOT, "HEAD")
    members = manifest["wheel"]["members"]

    assert "suno_dl/py.typed" in members
    shipped = {name for name in members if name.startswith("suno_dl/")}
    committed = _git(ROOT, "ls-tree", "-r", "--name-only", "HEAD", "--", "src/suno_dl")
    expected = {name.removeprefix("src/") for name in committed.splitlines()}
    assert shipped == expected


def test_manifest_serialises_to_stable_json() -> None:
    manifest = rm.build_manifest(ROOT, "HEAD")
    payload = json.dumps(manifest, indent=2, sort_keys=True)
    assert json.loads(payload)["wheel"]["sha256"] == manifest["wheel"]["sha256"]


def test_build_ignores_uncommitted_working_tree(tmp_path: Path) -> None:
    """A dirty checkout must never leak into the artefact."""
    repo = _synthetic_repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "src" / "suno_dl" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (repo / "src" / "suno_dl" / "untracked.py").write_text("dirty = True\n")

    manifest = rm.build_manifest(repo, "HEAD")
    assert "suno_dl/untracked.py" not in manifest["wheel"]["members"]

    sha, epoch = rm.commit_info(repo, "HEAD")
    tree = tmp_path / "tree"
    tree.mkdir()
    rm.export_tree(repo, sha, tree)
    wheel = rm.build_wheel(tree, tmp_path / "dist", epoch)
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("suno_dl/__init__.py") == b'__version__ = "0.0.1"\n'


def test_verify_members_rejects_a_stale_member(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "src" / "suno_dl").mkdir(parents=True)
    (tree / "src" / "suno_dl" / "mod.py").write_text("committed\n")
    wheel = tmp_path / "pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("suno_dl/mod.py", "stale\n")

    members = rm.wheel_members(wheel)
    with pytest.raises(rm.ManifestError, match="stale wheel member"):
        rm.verify_members(wheel, tree, members)


def test_verify_members_rejects_missing_and_extra_members(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "src" / "suno_dl").mkdir(parents=True)
    (tree / "src" / "suno_dl" / "a.py").write_text("a\n")
    wheel = tmp_path / "pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("suno_dl/b.py", "b\n")

    members = rm.wheel_members(wheel)
    with pytest.raises(rm.ManifestError, match="does not match the commit"):
        rm.verify_members(wheel, tree, members)


def test_commit_info_resolves_a_ref(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    sha, epoch = rm.commit_info(repo, "HEAD")
    assert len(sha) == 40
    assert epoch > 0


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rm.main(["--ref", "no-such-ref", "--repo", str(tmp_path)]) == 2


def test_exported_package_bytes_are_stable_with_autocrlf(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    sha, _ = rm.commit_info(repo, "HEAD")
    tree = tmp_path / "exported"
    tree.mkdir()
    rm.export_tree(repo, sha, tree)
    committed = subprocess.run(
        ["git", "show", "HEAD:src/suno_dl/__init__.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert committed == b'__version__ = "0.0.1"\n'
    assert (tree / "src" / "suno_dl" / "__init__.py").read_bytes() == committed


def _record_wheel(path: Path, *, corrupt: str | None = None) -> None:
    files = {"suno_dl/mod.py": b"source\n", "demo-1.dist-info/METADATA": b"Name: demo\n"}
    name = "demo-1.dist-info/RECORD"
    rows = []
    for member, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([member, "sha256=" + digest, str(len(data))])
    rows.append([name, "", ""])
    if corrupt == "hash":
        rows[0][1] = "sha256=invalid"
    elif corrupt == "size":
        rows[0][2] = "999"
    elif corrupt == "missing":
        rows.pop(0)
    elif corrupt == "duplicate":
        rows.append(rows[0])
    elif corrupt == "self":
        rows[-1][1] = "sha256=invalid"
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in files.items():
            archive.writestr(member, data)
        archive.writestr(name, output.getvalue())


@pytest.mark.parametrize("corrupt", ["hash", "size", "missing", "duplicate", "self"])
def test_record_integrity_rejects_corrupt_or_incomplete_rows(tmp_path, corrupt):
    wheel = tmp_path / "demo.whl"
    _record_wheel(wheel, corrupt=corrupt)
    with pytest.raises(rm.ManifestError, match="RECORD"):
        rm.verify_record(wheel, rm.wheel_members(wheel))


def test_record_integrity_accepts_a_complete_wheel(tmp_path):
    wheel = tmp_path / "demo.whl"
    _record_wheel(wheel)
    rm.verify_record(wheel, rm.wheel_members(wheel))


def test_wheel_rejects_duplicate_zip_members(tmp_path):
    wheel = tmp_path / "demo.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("suno_dl/mod.py", "one")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("suno_dl/mod.py", "two")
    with pytest.raises(rm.ManifestError, match="duplicate ZIP"):
        rm.wheel_members(wheel)


def test_existing_wheel_must_match_exact_reproducible_bytes(tmp_path):
    repo = _synthetic_repo(tmp_path)
    sha, epoch = rm.commit_info(repo, "HEAD")
    tree = tmp_path / "source"
    tree.mkdir()
    rm.export_tree(repo, sha, tree)
    wheel = rm.build_wheel(tree, tmp_path / "dist", epoch)
    manifest = rm.build_manifest(repo, "HEAD", wheel)
    assert manifest["wheel"]["existing_wheel_verified"] is True
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.comment = b"different bytes, same members"
    with pytest.raises(rm.ManifestError, match="bytes do not match"):
        rm.build_manifest(repo, "HEAD", wheel)


def test_manifest_refuses_nondeterministic_builds(tmp_path, monkeypatch):
    repo = _synthetic_repo(tmp_path)
    original = rm.build_wheel

    def build_with_second_variant(tree, out_dir, epoch):
        wheel = original(tree, out_dir, epoch)
        if out_dir.name == "dist2":
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.comment = b"nondeterministic output"
        return wheel

    monkeypatch.setattr(rm, "build_wheel", build_with_second_variant)
    with pytest.raises(rm.ManifestError, match="not byte-reproducible"):
        rm.build_manifest(repo, "HEAD")


def test_release_wheel_and_sdist_build_sequence_verifies_uploaded_wheel(tmp_path):
    repo = _synthetic_repo(tmp_path)
    sha, epoch = rm.commit_info(repo, "HEAD")
    tree = tmp_path / "exported"
    tree.mkdir()
    rm.export_tree(repo, sha, tree)
    out = tmp_path / "release-dist"
    out.mkdir()
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), PIP_NO_INDEX="1")
    for kind in ("--wheel", "--sdist"):
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", kind, "--outdir", str(out)],
            cwd=tree,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            umask=0o022 if os.name != "nt" else -1,
        )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1 and len(list(out.glob("*.tar.gz"))) == 1
    result = rm.build_manifest(repo, "HEAD", wheels[0])
    assert result["wheel"]["existing_wheel_verified"] is True


@pytest.mark.skipif(os.name == "nt", reason="Unix source permission masks")
def test_source_exports_and_wheels_are_stable_across_permission_masks(tmp_path):
    repo = _synthetic_repo(tmp_path)
    _git(repo, "config", "tar.umask", "0000")
    _git(repo, "update-index", "--chmod=+x", "src/suno_dl/__init__.py")
    _git(repo, "commit", "-qm", "executable source fixture")
    script = """
import importlib.util,os,sys,pathlib,hashlib
spec=importlib.util.spec_from_file_location("release_manifest",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
os.umask(int(sys.argv[4],8))
repo=pathlib.Path(sys.argv[2]);target=pathlib.Path(sys.argv[3])
target.mkdir();source=target/"source";source.mkdir()
sha,epoch=m.commit_info(repo,"HEAD")
m.export_tree(repo,sha,source)
assert (source/"src/suno_dl/__init__.py").stat().st_mode & 0o777 == 0o755
assert (source/"src/suno_dl/data/catalogue.json").stat().st_mode & 0o777 == 0o644
wheel=m.build_wheel(source,target/"dist",epoch)
print(hashlib.sha256(wheel.read_bytes()).hexdigest())
"""
    hashes = []
    for mask in ("0002", "0077"):
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(SCRIPT),
                str(repo),
                str(tmp_path / ("mask-" + mask)),
                mask,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=dict(os.environ, PIP_NO_INDEX="1"),
        )
        hashes.append(r.stdout.strip())
    assert hashes[0] == hashes[1]
