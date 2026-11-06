#!/usr/bin/env python3
"""Emit a verifiable release manifest for one pinned commit.

Why this exists (#163): the CT107 image and the GitHub package both call
themselves ``1.5.3`` while containing different bytes, an installed wheel once
silently shipped an unreadable default catalogue, and two builds of the same
commit produced different wheel hashes. None of that is detectable from a
version string, so releases need a manifest that ties a wheel to the exact
commit and file contents it was built from.

What it does, for a single git ref and nothing else:

1. Resolves the ref to a commit and reads its committed timestamp.
2. Exports that tree with ``git archive`` (never the local working tree).
3. Builds the wheel twice from independent exports using the commit timestamp,
   and refuses to assert reproducibility if the resulting bytes differ.
4. Compares package members against the exported files and validates RECORD.
5. Optionally requires an existing wheel to match those exact bytes.
6. Writes a JSON manifest: commit, wheel SHA-256, per-member SHA-256 map,
   metadata, dependency requirements and build toolchain.

Exit codes: 0 manifest written, 1 verification failed, 2 usage/infrastructure
problem (git, build backend, missing interpreter tooling).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Shipped members are compared byte-for-byte; anything else in the wheel
# (dist-info, RECORD) is recorded but not used as a provenance anchor.
PACKAGE_PREFIX = "suno_dl/"

# Directories that are build outputs, never part of a source tree export.
SKIP_DIRS = {"__pycache__", "build", "dist", ".git"}


class ManifestError(RuntimeError):
    """A release-blocking problem: the artefact does not match the commit."""


class ToolchainError(RuntimeError):
    """The local toolchain cannot produce a manifest (missing git/build)."""


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    umask: int = -1,
) -> str:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            umask=umask,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ToolchainError(f"missing required command: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise ToolchainError(
            f"{' '.join(cmd)} failed with exit {exc.returncode}: "
            f"{detail[-1] if detail else 'no output'}"
        ) from exc
    return completed.stdout.strip()


def commit_info(repo: Path, ref: str) -> tuple[str, int]:
    """Resolve ``ref`` to ``(sha, committed_epoch)``."""
    _run(["git", "rev-parse", "--git-dir"], cwd=repo)
    sha = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo)
    epoch = int(_run(["git", "log", "-1", "--format=%ct", sha], cwd=repo))
    return sha, epoch


def export_tree(repo: Path, sha: str, destination: Path) -> None:
    """Materialise exactly the committed tree, ignoring local working files."""
    archive = destination / "source.tar"
    # Pin Git archive permissions; repository configuration and process umask
    # must not change wheel data-file ZIP attributes.
    _run(
        ["git", "-c", "tar.umask=0022", "archive", "--format=tar", "-o", str(archive), sha],
        cwd=repo,
    )
    try:
        with tarfile.open(archive, "r:") as exported:
            exported.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise ToolchainError("cannot extract the committed source archive") from exc
    finally:
        archive.unlink(missing_ok=True)


def build_wheel(tree: Path, out_dir: Path, epoch: int) -> Path:
    """Build one wheel from ``tree`` with reproducible timestamps."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        _run(
            [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(out_dir)],
            cwd=tree,
            env=env,
            # This child creates dist-info and build intermediates. Never
            # mutate the parent's process-wide mask while other threads run.
            umask=0o022 if os.name != "nt" else -1,
        )
    except ToolchainError as exc:
        if "No module named build" in str(exc) or "Unmet dependencies" in str(exc):
            raise ToolchainError(
                "the build toolchain is incomplete; install it with "
                "`pip install 'trackie-assistant[dev]'` (needs build, setuptools, wheel)"
            ) from exc
        raise
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ToolchainError(f"expected exactly one wheel in {out_dir}, found {len(wheels)}")
    return wheels[0]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def wheel_members(wheel: Path) -> dict[str, dict[str, Any]]:
    """Per-member SHA-256 and size; reject corrupt or ambiguous ZIPs."""
    members: dict[str, dict[str, Any]] = {}
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ManifestError("wheel has duplicate ZIP member names")
            bad = archive.testzip()
            if bad is not None:
                raise ManifestError(f"wheel is corrupt: first bad member {bad}")
            for name in sorted(names):
                if name.endswith("/"):
                    continue
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ManifestError(f"unsafe wheel member name: {name}")
                payload = archive.read(name)
                members[name] = {"sha256": _sha256_bytes(payload), "size": len(payload)}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManifestError(f"wheel is unreadable: {wheel}") from exc
    return members


def verify_record(wheel: Path, members: dict[str, dict[str, Any]]) -> None:
    """Require one complete, unique SHA-256 RECORD matching the archive bytes."""
    records = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ManifestError("wheel must contain exactly one RECORD")
    record_name = records[0]
    try:
        with zipfile.ZipFile(wheel) as archive:
            text = archive.read(record_name).decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as exc:
        raise ManifestError("invalid wheel RECORD") from exc
    seen: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise ManifestError("invalid wheel RECORD row")
        name, encoded, size = row
        if name in seen or name not in members:
            raise ManifestError(f"duplicate or unknown wheel RECORD member: {name}")
        seen.add(name)
        if name == record_name:
            if encoded or size:
                raise ManifestError("RECORD must not hash itself")
            continue
        expected = (
            base64.urlsafe_b64encode(bytes.fromhex(members[name]["sha256"])).decode().rstrip("=")
        )
        if encoded != f"sha256={expected}" or size != str(members[name]["size"]):
            raise ManifestError(f"wheel RECORD mismatch: {name}")
    if seen != set(members):
        raise ManifestError("wheel RECORD does not list every member")


def verify_members(wheel: Path, tree: Path, members: dict[str, dict[str, Any]]) -> None:
    """Fail unless every shipped package member equals the committed file."""
    source_root = tree / "src" / "suno_dl"
    if not source_root.is_dir():
        raise ManifestError(f"no src/suno_dl in the exported tree ({tree})")
    expected = {
        PACKAGE_PREFIX + path.relative_to(source_root).as_posix(): path
        for path in source_root.rglob("*")
        if path.is_file() and not (set(path.parts) & SKIP_DIRS)
    }
    shipped = {name for name in members if name.startswith(PACKAGE_PREFIX)}
    missing = sorted(set(expected) - shipped)
    extra = sorted(shipped - set(expected))
    if missing or extra:
        raise ManifestError(
            f"wheel does not match the commit: missing={missing[:5]} extra={extra[:5]}"
        )
    with zipfile.ZipFile(wheel) as archive:
        for name, path in sorted(expected.items()):
            if archive.read(name) != path.read_bytes():
                raise ManifestError(f"stale wheel member: {name}")


def wheel_metadata(wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        if name is None:
            raise ManifestError("wheel has no dist-info/METADATA")
        message = BytesParser().parsebytes(archive.read(name))
        entry_name = next(
            (n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")), None
        )
        entry_points = (
            archive.read(entry_name).decode("utf-8").strip().splitlines()
            if entry_name is not None
            else []
        )
    return {
        "name": message["Name"],
        "version": message["Version"],
        "requires_python": message["Requires-Python"],
        "summary": message["Summary"],
        "requires_dist": sorted(message.get_all("Requires-Dist") or []),
        "entry_points": sorted(line.strip() for line in entry_points if line.strip()),
    }


def toolchain() -> dict[str, str]:
    versions: dict[str, str] = {"python": sys.version.split()[0]}
    for module in ("build", "setuptools", "wheel"):
        try:
            from importlib.metadata import version as dist_version

            versions[module] = dist_version(module)
        except Exception:  # noqa: BLE001 - reporting, never a failure
            versions[module] = "absent"
    return versions


def build_manifest(repo: Path, ref: str = "HEAD", wheel_path: Path | None = None) -> dict[str, Any]:
    """Verify reproducible wheels for a committed ref and optionally an existing wheel."""
    sha, epoch = commit_info(repo, ref)
    with tempfile.TemporaryDirectory(prefix="release-manifest-") as tmp:
        root = Path(tmp)
        wheels = []
        trees = []
        for index in (1, 2):
            tree = root / f"tree{index}"
            tree.mkdir()
            export_tree(repo, sha, tree)
            candidate = build_wheel(tree, root / f"dist{index}", epoch)
            members = wheel_members(candidate)
            verify_members(candidate, tree, members)
            verify_record(candidate, members)
            wheels.append(candidate)
            trees.append(tree)
        if wheels[0].name != wheels[1].name or _sha256_file(wheels[0]) != _sha256_file(wheels[1]):
            raise ManifestError("independent wheel builds are not byte-reproducible")
        wheel = wheels[0]
        members = wheel_members(wheel)
        if wheel_path is not None:
            external_members = wheel_members(wheel_path)
            verify_record(wheel_path, external_members)
            verify_members(wheel_path, trees[0], external_members)
            if wheel_path.name != wheel.name or _sha256_file(wheel_path) != _sha256_file(wheel):
                raise ManifestError(
                    "existing wheel bytes do not match the reproducible commit build"
                )
        metadata = wheel_metadata(wheel)
        return {
            "schema": SCHEMA_VERSION,
            "commit": {"sha": sha, "committed_epoch": epoch, "ref": ref},
            "wheel": {
                "filename": wheel.name,
                "sha256": _sha256_file(wheel),
                "size": wheel.stat().st_size,
                "reproducible": True,
                "reproducible_via": "two independent builds with SOURCE_DATE_EPOCH=<commit epoch>",
                "existing_wheel_verified": wheel_path is not None,
                "members": members,
            },
            "package": metadata,
            "toolchain": toolchain(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="HEAD", help="git ref to describe (default HEAD)")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="manifest path (default: stdout)",
    )
    parser.add_argument(
        "--verify-wheel",
        type=Path,
        default=None,
        help="require this existing wheel to match the reproducible committed build",
    )
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest(args.repo.resolve(), args.ref, args.verify_wheel)
    except ToolchainError as exc:
        print(f"release-manifest: toolchain error: {exc}", file=sys.stderr)
        return 2
    except ManifestError as exc:
        print(f"release-manifest: verification failed: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.out is None:
        sys.stdout.write(payload)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
        print(
            f"release-manifest: {manifest['commit']['sha'][:12]} "
            f"{manifest['wheel']['filename']} sha256={manifest['wheel']['sha256']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
