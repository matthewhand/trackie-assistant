"""ffmpeg discovery, shared by the audio-extraction and tagging paths.

Kept in its own module so :mod:`suno_dl.tagging` and :mod:`suno_dl.downloader`
can both reach it without importing each other.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg executable path, or ``None``.

    ``SUNO_FFMPEG`` overrides PATH discovery; an override that is not an
    executable file is refused (a silent fallback to a different binary would
    be worse than no tagging at all).
    """
    if (ffmpeg := shutil.which("ffmpeg")) is None:
        return None
    # When ffmpeg is absent the env var is irrelevant; if it is present, an
    # explicit override must point at a real executable or we refuse.
    override = os.environ.get("SUNO_FFMPEG")
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        logger.warning("SUNO_FFMPEG=%s is not an executable file; ignoring", override)
    return ffmpeg
