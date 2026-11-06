"""Linux kernel mountinfo safety parser: no mounting or privileged actions."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Linux-only manual CI deletion guard uses POSIX paths"
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci-mount-guard.py"
spec = importlib.util.spec_from_file_location("ci_mount_guard", MODULE_PATH)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def record(mountpoint: bytes) -> bytes:
    return b"42 1 0:99 / " + mountpoint + b" rw - ext4 /dev/fake rw\n"


def listing(*mounts: bytes) -> io.BytesIO:
    return io.BytesIO(b"".join(record(p) for p in mounts))


def test_detects_nested_mount_beneath_plain_directory() -> None:
    # Neither parent is itself a mountpoint. findmnt -R used to miss this.
    assert guard.has_workspace_mount(
        "/scratch/work",
        listing(b"/", b"/scratch/work/subdir/bind", b"/another"),
    )


@pytest.mark.parametrize(
    "mount",
    [b"/scratch/work", b"/scratch/work/subdir", b"/scratch/work/subdir/deeper"],
)
def test_detects_exact_or_descendant_mount(mount: bytes) -> None:
    assert guard.has_workspace_mount("/scratch/work", listing(b"/", mount))


def test_component_boundary_and_parent_mount_not_false_positives() -> None:
    assert not guard.has_workspace_mount(
        "/scratch/work",
        listing(b"/", b"/scratch", b"/scratch/work2", b"/scratch/work-space"),
    )


@pytest.mark.parametrize(
    ("escaped_mount", "workspace"),
    [
        (rb"/scratch/space\040name/work/bind", "/scratch/space name/work"),
        (rb"/scratch/tab\011name/work/bind", "/scratch/tab\tname/work"),
        (rb"/scratch/newline\012name/work/bind", "/scratch/newline\nname/work"),
        (rb"/scratch/backslash\134name/work/bind", r"/scratch/backslash\name/work"),
    ],
)
def test_octal_mountpoint_escapes_detect_nested_mount(escaped_mount: bytes, workspace: str) -> None:
    assert guard.has_workspace_mount(workspace, listing(b"/", escaped_mount))


def test_literal_backslash_mountpoint_is_not_confused_with_escape() -> None:
    assert guard._unescape_mountpoint(rb"/literal\134backslash") == b"/literal\\backslash"


@pytest.mark.parametrize(
    "mount",
    [rb"/scratch/invalid\04x", rb"/scratch/invalid\q", b"relative/mount"],
)
def test_malformed_mountpoint_is_fail_closed(mount: bytes) -> None:
    with pytest.raises(ValueError):
        guard.has_workspace_mount("/scratch/work", listing(mount))


@pytest.mark.parametrize("data", [b"", b"bad line\n", b"42 1 0:99 / /work rw\n"])
def test_empty_or_malformed_mountinfo_is_fail_closed(data: bytes) -> None:
    with pytest.raises(ValueError):
        guard.has_workspace_mount("/scratch/work", io.BytesIO(data))


def test_unreadable_mountinfo_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_open(*args: object, **kwargs: object) -> object:
        raise OSError("synthetic unreadable kernel mount table")

    monkeypatch.setattr("builtins.open", fail_open)
    assert guard.main(["guard", "/scratch/work"]) == 4


def test_mount_guard_live_kernel_is_read_only(tmp_path: Path) -> None:
    if not Path("/proc/self/mountinfo").is_file():
        pytest.skip("Linux kernel mountinfo is unavailable on this platform")
    # Synthetic workspace is ordinary and has no child mount.
    assert guard.main(["guard", str(tmp_path / "unused")]) == 0


def test_ci_calls_guard_before_and_inside_lock() -> None:
    script = (MODULE_PATH.parent / "ci-local.sh").read_text()
    assert script.count('python3 "$MOUNT_GUARD" "$WORK" || exit $?') == 2
    assert script.index('python3 "$MOUNT_GUARD" "$WORK" || exit $?') < script.index(
        "exec flock -n -o -E 75"
    )
    assert script.rindex('python3 "$MOUNT_GUARD" "$WORK" || exit $?') < script.index(
        'rm -rf --one-file-system -- "$WORK"'
    )
    assert "findmnt -rn -o TARGET -R" not in script


@pytest.mark.parametrize(
    "invalid",
    [
        b"not-a-number 1 0:99 / /other rw - ext4 /dev/fake rw\n",
        b"42 broken 0:99 / /other rw - ext4 /dev/fake rw\n",
        b"42 1 invalid / /other rw - ext4 /dev/fake rw\n",
        b"42 1 0:99 relative /other rw - ext4 /dev/fake rw\n",
        b"42 1 0:99 / /other rw - ext4 /dev/fake\n",
        b"42 1 0:99 / /other rw - ext4 /dev/fake rw extra\n",
        b"42 1 0:99 / /other rw - ext4 /dev/fake rw - x y z\n",
        rb"42 1 0:99 /invalid\q /other rw - ext4 /dev/fake rw" + b"\n",
        b"42 1 0:99 / /other rw - ext4 /dev/fake rw\x00\n",
    ],
)
def test_rejects_structurally_malformed_full_mount_records(invalid: bytes) -> None:
    with pytest.raises(ValueError):
        guard.has_workspace_mount("/scratch/work", io.BytesIO(invalid))


def test_mount_match_does_not_hide_later_malformed_record() -> None:
    data = (
        record(b"/scratch/work/mounted")
        + b"nonsense parent dev root /other rw - ext4 /dev/fake rw\n"
    )
    with pytest.raises(ValueError):
        guard.has_workspace_mount("/scratch/work", io.BytesIO(data))


def test_valid_optional_mount_fields_are_accepted() -> None:
    data = b"42 1 0:99 / /scratch/other rw shared:7 master:1 - ext4 /dev/fake rw\n"
    assert guard.has_workspace_mount("/scratch/work", io.BytesIO(data)) is False


def test_valid_linux_nsfs_mount_root_is_accepted() -> None:
    data = b"42 1 0:99 mnt:[4026533444] /run/snapd/ns/lxd.mnt rw - nsfs mnt:[4026533444] rw\n"
    assert guard.has_workspace_mount("/scratch/work", io.BytesIO(data)) is False
