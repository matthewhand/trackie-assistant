"""Small synthetic regression tests for the *manual* CI scratch-path guard (#121)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-local.sh"


def _invoke(
    tmp_path: Path, workspace: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        CI_WORKDIR=str(workspace),
        CI_LOGDIR=str(tmp_path / "logs"),
        # Prevent reaching the clone operation even if a guard regresses:
        # the uv binary is intentionally excluded from this PATH.
        PATH="/usr/bin:/bin",
    )
    env.pop("SUNO_CI_LOCKED", None)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), "master"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=4,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_ci_rejects_workspace_outside_dedicated_scratch(tmp_path: Path) -> None:
    protected = tmp_path / "do-not-delete"
    protected.mkdir()
    sentinel = protected / "sentinel"
    sentinel.write_text("keep")
    result = _invoke(tmp_path, protected)
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert sentinel.read_text() == "keep"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_ci_rejects_symlinked_scratch_path(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "sentinel").write_text("keep")
    workspace = root / "work"
    workspace.symlink_to(protected, target_is_directory=True)
    result = _invoke(tmp_path, workspace)
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert (protected / "sentinel").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_ci_refuses_overlapping_runs(tmp_path: Path) -> None:
    """Existing scratch files survive when the global CI lock is held."""
    import fcntl

    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    sentinel = workspace / "sentinel"
    sentinel.write_text("keep")
    lock = root / ".ci-local.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _invoke(tmp_path, workspace)
        assert result.returncode == 75, (result.stdout, result.stderr)
    assert sentinel.read_text() == "keep"


def test_one_workspace_for_all_refs_and_global_lock() -> None:
    content = SCRIPT.read_text()
    assert 'WORK="${CI_WORKDIR:-$CI_ROOT/work}"' in content
    assert "work-${REF" not in content
    assert "flock -n -o -E 75" in content
    assert 'rm -rf --one-file-system -- "$WORK"' in content
    assert 'PYTHONS="${CI_PYTHONS:-3.13}"' in content


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_env_marker_cannot_bypass_existing_lock(tmp_path: Path) -> None:
    """The caller must not forge exclusive ownership via SUNO_CI_LOCKED."""
    import fcntl

    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    sentinel = workspace / "sentinel"
    sentinel.write_text("keep")
    with (root / ".ci-local.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _invoke(tmp_path, workspace, {"SUNO_CI_LOCKED": "1"})
        assert result.returncode == 75, (result.stdout, result.stderr)
    assert sentinel.read_text() == "keep"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_lockfile_symlink_rejected(tmp_path: Path) -> None:
    """A lock must not be opened through an unexpected symlink."""
    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    protected = tmp_path / "protected-lock"
    protected.write_text("safe")
    (root / ".ci-local.lock").symlink_to(protected)
    result = _invoke(tmp_path, workspace)
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert protected.read_text() == "safe"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_close_flag_does_not_leave_descendant_holding_lock(tmp_path: Path) -> None:
    """A short-lived synthetic child must not extend the supervisor lock."""
    lock = tmp_path / "synthetic.lock"
    command = [
        "flock",
        "-n",
        "-o",
        "-E",
        "75",
        str(lock),
        "bash",
        "-c",
        "sleep 0.4 </dev/null >/dev/null 2>&1 & exit 0",
    ]
    acquired = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    assert acquired.returncode == 0, acquired.stderr
    renewed = subprocess.run(
        ["flock", "-n", "-E", "75", str(lock), "true"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert renewed.returncode == 0, renewed.stderr
    assert "flock -n -o -E 75" in SCRIPT.read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_locked_worker_preflight_does_not_touch_workspace(tmp_path: Path) -> None:
    """Without uv, a supervised worker must exit before destructive checkout."""
    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    sentinel = workspace / "sentinel"
    sentinel.write_text("keep")
    result = _invoke(tmp_path, workspace)
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert sentinel.read_text() == "keep"
    # Supervisor exited, lock released. A second independent lock can take over.
    available = subprocess.run(
        ["flock", "-n", "-E", "75", str(root / ".ci-local.lock"), "true"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert available.returncode == 0, available.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_ci_refuses_missing_python3(tmp_path: Path) -> None:
    """Mount inspection cannot be skipped if system Python is unavailable."""
    tools = tmp_path / "only-shell"
    tools.mkdir()
    for name in ("bash", "dirname"):
        (tools / name).symlink_to(Path("/usr/bin") / name)
    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    sentinel = workspace / "sentinel"
    sentinel.write_text("keep")
    result = _invoke(tmp_path, workspace, {"PATH": str(tools)})
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "python3 required" in result.stderr
    assert sentinel.read_text() == "keep"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only manual CI helper")
def test_second_mount_check_blocks_checkout(tmp_path: Path) -> None:
    """A newly detected nested mount prevents the destructive reset under lock."""
    root = tmp_path / "home" / ".local" / "state" / "suno-ci-local"
    root.mkdir(parents=True)
    workspace = root / "work"
    workspace.mkdir()
    sentinel = workspace / "sentinel"
    sentinel.write_text("keep")

    tools = tmp_path / "test-tools"
    tools.mkdir()
    check_count = tmp_path / "mount-check-count"
    wrapper = tools / "python3"
    wrapper.write_text(
        "#!/bin/bash\n"
        'count=$(cat "$MOUNT_CHECK_COUNT" 2>/dev/null || echo 0)\n'
        "count=$((count + 1))\n"
        'echo "$count" > "$MOUNT_CHECK_COUNT"\n'
        'if [ "$count" -eq 2 ]; then exit 78; fi\n'
        'exec /usr/bin/python3 "$@"\n'
    )
    wrapper.chmod(0o755)
    no_op_uv = tools / "uv"
    no_op_uv.write_text("#!/bin/bash\nexit 99\n")
    no_op_uv.chmod(0o755)
    result = _invoke(
        tmp_path,
        workspace,
        {
            "PATH": str(tools) + ":/usr/bin:/bin",
            "MOUNT_CHECK_COUNT": str(check_count),
        },
    )
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert check_count.read_text().strip() == "2"
    assert sentinel.read_text() == "keep"
