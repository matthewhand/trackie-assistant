#!/usr/bin/env python3
"""Refuse a release while required production capability guards are active.

Run after installing the package on every release matrix platform. This checks
known hard stops; success is not evidence that all release review is complete.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable


def active_blockers(checks: Iterable[tuple[str, Callable[[], None], type[Exception]]]) -> list[str]:
    """Return known capability blocks; unexpected failures must also stop release."""
    blockers = []
    for name, check, expected in checks:
        try:
            check()
        except expected:
            blockers.append(name)
    return blockers


def main() -> int:
    # Imports must succeed: a broken release environment is not a pass.
    from suno_dl.archive_lock import raise_if_unsupported_archive_write
    from suno_dl.download_intent import WindowsArchiveWriteUnavailable
    from suno_dl.linux_downloads import require_audio_sync
    from suno_dl.producer_lookup import ProducerLookupError, _require_trusted_framing
    from suno_dl.tagging import TaggingUnavailable

    blockers = active_blockers(
        [
            ("producer verification", _require_trusted_framing, ProducerLookupError),
            ("safe Linux audio sync", require_audio_sync, TaggingUnavailable),
            (
                "archive writes on this platform",
                raise_if_unsupported_archive_write,
                WindowsArchiveWriteUnavailable,
            ),
        ]
    )
    if blockers:
        print("Release blocked: " + ", ".join(blockers), file=sys.stderr)
        return 1
    print("Known capability guards are open; complete all other release checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
