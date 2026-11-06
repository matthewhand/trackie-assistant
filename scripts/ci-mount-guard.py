#!/usr/bin/env python3
"""Fail-closed Linux mount boundary guard for Suno CI scratch deletion.

Inspect every entry in /proc/self/mountinfo, not findmnt -R <directory>.
The latter traverses mount trees only when the queried path is a mountpoint.
Linux mountinfo escapes space, tab, newline and backslash in path fields using
backslash + three octal digits.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable

_OCTAL_ESCAPE = re.compile(rb"\\([0-7]{3})")


def _unescape_mountpoint(field: bytes) -> bytes:
    """Decode mountinfo's octal-escaped mountpoint as filesystem bytes."""
    if not field.startswith(b"/"):
        raise ValueError("mountpoint is not absolute")
    decoded = _OCTAL_ESCAPE.sub(lambda match: bytes((int(match[1], 8),)), field)
    # A decoded literal backslash is legal; reject *unconsumed input* escapes.
    if b"\\" in _OCTAL_ESCAPE.sub(b"", field):
        raise ValueError("invalid mountinfo escape")
    if b"\x00" in decoded:
        raise ValueError("NUL in mountpoint")
    return decoded


def has_workspace_mount(workspace: str, lines: Iterable[bytes]) -> bool:
    """Validate *all* mount records and detect exact/descendant mountpoints."""
    root = os.fsencode(os.path.abspath(workspace)).rstrip(b"/") or b"/"
    prefix = root if root == b"/" else root + b"/"
    count = 0
    found = False
    for line in lines:
        count += 1
        record = line.rstrip(b"\n")
        if b"\x00" in record or record.count(b" - ") != 1:
            raise ValueError("malformed mountinfo record")
        before, after = record.split(b" - ", 1)
        fields = before.split()
        trailer = after.split()
        # id parent major:minor root mountpoint options [optional] - fstype source superoptions
        if len(fields) < 6 or len(trailer) != 3:
            raise ValueError("malformed mountinfo record")
        if not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError("malformed mount ID")
        if not re.fullmatch(rb"[0-9]+:[0-9]+", fields[2]):
            raise ValueError("malformed major:minor device")
        # Namespace mounts legitimately use roots like mnt:[4026533444].
        # Ordinary filesystems must still provide an absolute escaped root.
        if fields[3].startswith(b"/"):
            _unescape_mountpoint(fields[3])
        elif trailer[0] != b"nsfs" or not re.fullmatch(rb"[a-z_]+:\[[0-9]+\]", fields[3]):
            raise ValueError("malformed mount root")
        mountpoint = _unescape_mountpoint(fields[4])
        if not fields[5] or not trailer[0] or not trailer[1] or not trailer[2]:
            raise ValueError("malformed mount options / filesystem trailer")
        found = found or mountpoint == root or mountpoint.startswith(prefix)
    if count == 0:
        raise ValueError("empty mountinfo")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("FATAL: expected one CI workspace argument", file=sys.stderr)
        return 4
    try:
        with open("/proc/self/mountinfo", "rb") as mountinfo:
            if has_workspace_mount(argv[1], mountinfo):
                print("FATAL: CI scratch path contains a mount", file=sys.stderr)
                return 78
    except (OSError, ValueError, UnicodeError) as exc:
        print(
            f"FATAL: CI mount inspection unavailable or malformed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
