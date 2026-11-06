#!/usr/bin/env python3
"""Fail closed on unsafe manual-CI log paths, without creating any files."""

from __future__ import annotations

import os
import stat
import sys


def validate_logdir(workspace: str, logdir: str) -> None:
    """Keep logs outside the disposable worktree and reject symlink aliases.

    This is a preflight, not an atomic defence against same-user path swaps.
    """
    if not os.path.isabs(logdir) or "\x00" in logdir:
        raise ValueError("CI_LOGDIR must be an absolute path")
    parts = logdir.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("CI_LOGDIR contains an ambiguous path component")
    if not parts:
        raise ValueError("CI_LOGDIR cannot be filesystem root")

    # Inspect every path component, including parents that already exist.
    # Do not follow an existing symlink or accept a regular-file component.
    current = "/"
    for part in parts:
        current = os.path.join(current, part)
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError("CI_LOGDIR has a symlink or non-directory component")

    work = os.path.realpath(workspace)
    logs = os.path.realpath(logdir)
    if os.path.commonpath((work, logs)) == work:
        raise ValueError("CI_LOGDIR cannot be within the disposable CI workspace")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("FATAL: expected CI workspace and log directory", file=sys.stderr)
        return 4
    try:
        validate_logdir(argv[1], argv[2])
    except (OSError, ValueError) as exc:
        print(f"FATAL: unsafe CI_LOGDIR: {type(exc).__name__}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
