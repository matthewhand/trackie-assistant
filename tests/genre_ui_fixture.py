"""Synthetic library + browser helpers for the genre UI viewport/a11y tests.

Everything here is invented: titles, styles and ids are generated from word
lists, never taken from a real Suno library. A handful of titles are
deliberately awkward (very long, unbroken, markup-looking, odd spacing) to prove
the card shows stored titles verbatim and never alters them.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from suno_dl.webui import RunManager, _make_handler

HARNESS = Path(__file__).parent / "fixtures" / "genre_card_harness.html"
CATEGORY = "Neo Soul & House"
N_SONGS = 30

LONG_TITLE = (
    "The Extraordinarily Long Synthetic Title That Keeps Going And Going Across "
    "Every Viewport We Test Because Truncation Must Never Change Stored Text"
)
UNBROKEN_TITLE = "Supercalifragilisticexpialidocious" * 3
ODD_TITLES = {
    1: "  Double   spaced  <b>markup</b> & \"quotes\" 'single'  ",
    3: LONG_TITLE,
    5: UNBROKEN_TITLE,
    7: "Ünïcödé — 夜のドライブ ✦",
}

_ADJ = ["Velvet", "Neon", "Paper", "Quiet", "Golden", "Static", "Hollow", "Amber"]
_NOUN = ["Garden", "Signal", "Orbit", "Harbor", "Lantern", "Engine", "Meadow", "Circuit"]


def synthetic_title(i: int) -> str:
    return ODD_TITLES.get(i) or f"{_ADJ[i % len(_ADJ)]} {_NOUN[(i // 3) % len(_NOUN)]} {i}"


def build_library() -> tuple[dict, list[dict]]:
    clips: list[dict] = []
    assignments: dict[str, str] = {}

    def add(prefix: str, n: int, style: str, cat: str, titles: bool = False) -> None:
        for i in range(n):
            cid = f"{prefix}-{i:04d}"
            clips.append(
                {
                    "id": cid,
                    "title": synthetic_title(i) if titles else f"{prefix.title()} Demo {i}",
                    "style": f"{style}, synthetic fixture {i}",
                    "model": "v4",
                }
            )
            assignments[cid] = cat

    add("aaaaaaaa", N_SONGS, "neo soul, warm keys", CATEGORY, titles=True)
    for k in range(0, N_SONGS, 2):
        clips[k]["style"] = "deep house, 4/4 groove, synthetic fixture"
    add("bbbbbbbb", 6, "industrial", "Cyber Industrial")
    add("cccccccc", 10, "synthwave night drive", "Synthwave: Night Drive")
    add("dddddddd", 8, "synthwave neon rain", "Synthwave: Neon Rain")
    add("eeeeeeee", 18, "lo-fi study beats", "Lo-fi Study Beats")
    add("ffffffff", 9, "unknown", "Uncategorised")
    genres = {
        "categories": [
            {"name": CATEGORY, "count": N_SONGS},
            {"name": "Cyber Industrial", "count": 6},
            {
                "name": "Synthwave",
                "count": 18,
                "children": [
                    {"name": "Synthwave: Night Drive", "count": 10},
                    {"name": "Synthwave: Neon Rain", "count": 8},
                ],
            },
            {"name": "Lo-fi Study Beats", "count": 18},
            {"name": "Uncategorised", "count": 9},
        ],
        "assignments": assignments,
        "pinned": [],
    }
    return genres, clips


def write_library(root: Path) -> Path:
    genres_dir = root / "genres"
    genres_dir.mkdir()
    genres, clips = build_library()
    (genres_dir / "genres.json").write_text(json.dumps(genres), encoding="utf-8")
    (genres_dir / "clips.json").write_text(json.dumps(clips), encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")
    return genres_dir


def serve(root: Path):
    """Start the real web UI handler over a synthetic library; returns (httpd, base_url)."""
    genres_dir = write_library(root)
    manager = RunManager(root)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(root, manager, genres_dir=genres_dir)
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
