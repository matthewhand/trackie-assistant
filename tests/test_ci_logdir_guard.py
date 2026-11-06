"""Non-destructive CI logging path validation for the manual CI helper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Linux-only manual CI deletion guard uses POSIX paths"
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "ci-logdir-guard.py"
spec = importlib.util.spec_from_file_location("ci_logdir_guard", SOURCE)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_default_logdir_outside_worktree_is_safe_and_not_created(tmp_path: Path) -> None:
    workspace = tmp_path / "home" / ".local" / "state" / "suno-ci-local" / "work"
    logdir = workspace.parent / "logs"
    guard.validate_logdir(str(workspace), str(logdir))
    assert not logdir.exists()


@pytest.mark.parametrize("suffix", ["", "/logs", "/subdir/logs"])
def test_logdir_at_or_under_workspace_rejected(tmp_path: Path, suffix: str) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(ValueError, match="within"):
        guard.validate_logdir(str(workspace), str(workspace) + suffix)


def test_adjacent_directory_is_not_false_positive(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    guard.validate_logdir(str(workspace), str(tmp_path / "workspace-logs"))


@pytest.mark.parametrize("alias", [".", "..", "//", "/../logs", "/./logs"])
def test_ambiguous_logdir_components_rejected(tmp_path: Path, alias: str) -> None:
    work = tmp_path / "work"
    path = str(tmp_path / "logs") + ("/" + alias if alias in (".", "..") else alias)
    with pytest.raises(ValueError, match="ambiguous"):
        guard.validate_logdir(str(work), path)


@pytest.mark.parametrize("path", ["logs", ".", "/", ""])
def test_nonabsolute_or_root_logdir_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        guard.validate_logdir(str(tmp_path / "work"), path)


def test_direct_logdir_symlink_rejected(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    alias = tmp_path / "log-alias"
    alias.symlink_to(work, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        guard.validate_logdir(str(work), str(alias))


def test_parent_symlink_to_workspace_rejected(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    alias = tmp_path / "log-parent"
    alias.symlink_to(work, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        guard.validate_logdir(str(work), str(alias / "logs"))


def test_regular_file_parent_rejected(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("keep")
    with pytest.raises(ValueError, match="non-directory"):
        guard.validate_logdir(str(tmp_path / "work"), str(file / "logs"))
    assert file.read_text() == "keep"


def test_main_returns_bounded_error_without_creating_logs(tmp_path: Path) -> None:
    work = tmp_path / "work"
    unsafe = work / "logs"
    assert guard.main(["ci-logdir-guard", str(work), str(unsafe)]) == 78
    assert not work.exists()
    assert guard.main(["ci-logdir-guard"]) == 4


def test_ci_script_has_log_guard_twice_without_running_it() -> None:
    script = (ROOT / "scripts" / "ci-local.sh").read_text()
    command = 'python3 "$LOG_GUARD" "$WORK" "$LOGDIR" || exit $?'
    assert script.count(command) == 2
    assert script.index(command) < script.index("exec flock -n -o -E 75")
    assert script.rindex(command) < script.index('mkdir -p "$LOGDIR"')
    assert '"$MOUNT_GUARD" "$LOG_GUARD"' in script
