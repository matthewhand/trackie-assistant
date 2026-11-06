"""Pure-function checks for the shared genre card/web page status helpers (#95).

Runs ``tests/js/genre_status.mjs`` under Node (the helpers have no DOM
dependencies); skipped where Node is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CORE = (
    Path(__file__).resolve().parents[1] / "src" / "suno_dl" / "webui_static" / "suno-genre-core.js"
)
SCRIPT = Path(__file__).parent / "js" / "genre_status.mjs"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_status_helpers_under_node() -> None:
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [NODE, str(SCRIPT), str(CORE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("ok")
