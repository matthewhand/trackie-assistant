"""Read-only pages of the live archive, keyed by validated song UUIDs."""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .library import LibraryIndex
from .sidecar import read_sidecar

PAGE_SIZE = 50
_AUDIO = (".m4a", ".mp3", ".wav")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _title_key(title: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", title.casefold())
        if not unicodedata.combining(ch)
    )


def _bpm(style: str) -> int:
    found = re.search(r"([0-9]{2,3})\s*BPM", style, re.I)
    value = int(found[1]) if found else 0
    return value if 40 <= value <= 300 else 0


def _stamp(value: object) -> float | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


class SongCatalogue:
    """Cache a read-only archive scan briefly so paging doesn't rescan the disk."""

    def __init__(self, root: Path, genres_dir: Path | None, genres_url: str = "") -> None:
        self.root = root
        self.genres_dir = genres_dir
        self.genres_url = genres_url
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        self._genres: dict[str, Any] = {}
        self._audio: dict[str, Path] = {}
        self._expires = 0.0

    def _refresh(self) -> None:
        genres: dict[str, Any] = {}
        extras: dict[str, dict[str, Any]] = {}
        if self.genres_dir is not None:
            data = _read(self.genres_dir / "genres.json")
            if isinstance(data, dict):
                genres = data
            clips = _read(self.genres_dir / "clips.json")
            if not isinstance(clips, list):
                clips = _read(self.genres_dir / "clips.slim.json")
            if isinstance(clips, list):
                extras = {
                    c["id"].lower(): c
                    for c in clips
                    if isinstance(c, dict) and isinstance(c.get("id"), str)
                }
        if self.genres_url:
            # Home Assistant keeps its editable taxonomy, while songs and audio
            # always come from this archive. A temporary HA outage keeps the last copy.
            genres = self._genres
            try:
                with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as client:
                    with client.stream("GET", self.genres_url) as response:
                        response.raise_for_status()
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > 1024 * 1024:
                                raise ValueError("Genre file too large")
                            body.extend(chunk)
                    data = json.loads(body)
                    if isinstance(data, dict):
                        genres = data
            except (httpx.HTTPError, ValueError):
                pass
        audio = {}
        rows = []
        for entry in LibraryIndex(self.root).scan().entries():
            clip_id = entry.clip_id
            raw = read_sidecar(entry.sidecar, strict_identity=True)
            if raw is None or raw["id"].lower() != clip_id:
                continue
            info = extras.get(clip_id, {})
            path = next(
                (path for ext in _AUDIO for path in entry.media if path.suffix.lower() == ext),
                None,
            )
            created = raw.get("created_at") or info.get("created_at") or info.get("created")
            stamp = _stamp(created)
            source = "suno" if stamp is not None else None
            if path is not None:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                audio[clip_id] = path
                if stamp is None:
                    stamp = mtime
                    created = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")
                    source = "file"
            tags = raw.get("tags")
            if isinstance(tags, list):
                tags = ", ".join(t for t in tags if isinstance(t, str))
            style = info.get("style") or raw.get("style") or tags
            duration = raw.get("duration_sec")
            rows.append(
                {
                    "id": clip_id,
                    "title": str(raw.get("title") or info.get("title") or entry.base.name),
                    "style": style if isinstance(style, str) else "",
                    "model": str(raw.get("model_name") or info.get("model") or ""),
                    "track": {
                        "audio": f"/api/songs/{clip_id}/audio" if path is not None else None,
                        "duration": duration if type(duration) in (int, float) else None,
                        "created": created if stamp is not None else None,
                        "created_source": source,
                    },
                    "stamp": stamp if stamp is not None else float("-inf"),
                }
            )
        self._rows, self._genres, self._audio = rows, genres, audio
        self._expires = time.monotonic() + 15

    def _snapshot(
        self, *, refresh: bool = False
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Path]]:
        with self._lock:
            if refresh or time.monotonic() >= self._expires:
                self._refresh()
            return self._rows, self._genres, self._audio

    def audio(self, clip_id: str) -> Path | None:
        _, _, files = self._snapshot()
        path = files.get(clip_id.lower())
        if path is None:
            return None
        try:
            resolved = path.resolve(strict=True)
            if (
                resolved != path
                or not resolved.is_relative_to(self.root.resolve())
                or not resolved.is_file()
            ):
                return None
        except OSError:
            return None
        return resolved

    def page(
        self,
        *,
        page: int,
        search: str,
        category: str,
        sort: str,
        pinned: bool,
        unsorted: bool,
        audio: bool,
        blocked: int | None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        rows, raw_genres, _ = self._snapshot(refresh=refresh)
        raw_assignments = raw_genres.get("assignments")
        assignments = raw_assignments if isinstance(raw_assignments, dict) else {}
        raw_placement = raw_genres.get("placement")
        placement = raw_placement if isinstance(raw_placement, dict) else {}
        raw_pins = raw_genres.get("pinned")
        pins = {p for p in raw_pins if isinstance(p, str)} if isinstance(raw_pins, list) else set()
        raw_categories = raw_genres.get("categories")
        categories = (
            [c for c in raw_categories if isinstance(c, dict) and isinstance(c.get("name"), str)]
            if isinstance(raw_categories, list)
            else []
        )
        parents = {}
        for item in categories:
            for child in item.get("children") or []:
                if isinstance(child, dict) and isinstance(child.get("name"), str):
                    parents[child["name"]] = item["name"]

        def leaf_for(clip_id: str) -> str:
            leaf = assignments.get(clip_id)
            return leaf if isinstance(leaf, str) else ""

        def is_unsorted(clip_id: str, leaf: str) -> bool:
            return leaf in ("", "Uncategorised") or "abstain" in str(placement.get(clip_id)).lower()

        query = search.strip().lower()
        matching = []
        facets: Counter[str] = Counter()
        totals: Counter[str] = Counter()
        leaves: Counter[str] = Counter()
        category_stats: Counter[str] = Counter()
        mix: Counter[str] = Counter()
        for row in rows:
            clip_id = row["id"]
            leaf = leaf_for(clip_id)
            flags = {
                "pinned": clip_id in pins,
                "unsorted": is_unsorted(clip_id, leaf),
                "audio": bool(row["track"]["audio"]),
            }
            totals.update(k for k, active in flags.items() if active)
            if leaf:
                leaves[leaf] += 1
            if (
                category
                and category != "All"
                and leaf != category
                and parents.get(leaf) != category
            ):
                continue
            category_stats["tracks"] += 1
            category_stats["pinned"] += int(flags["pinned"])
            placed = str(placement.get(clip_id) or "").lower()
            bucket = "other"
            if "abstain" in placed:
                bucket = "abstained"
            elif placed.startswith("system1"):
                bucket = "system1"
            elif "llm" in placed:
                bucket = "llm"
            elif "manual" in placed:
                bucket = "manual"
            mix[bucket] += 1
            mix["pinned"] += int(flags["pinned"])
            if query and query not in (row["title"] + " " + row["style"]).lower():
                continue
            facets["total"] += 1
            facets.update(k for k, active in flags.items() if active)
            if (pinned and not flags["pinned"]) or (unsorted and not flags["unsorted"]):
                continue
            if audio and not flags["audio"]:
                continue
            matching.append(row)

        def sort_key(row: dict[str, Any]) -> tuple[str | float, str]:
            if sort == "az":
                return _title_key(row["title"]), row["id"]
            if sort == "bpm":
                return -_bpm(row["style"]), row["id"]
            return -row["stamp"], row["id"]

        matching.sort(key=sort_key)
        total = len(matching)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        start = (page - 1) * PAGE_SIZE
        selected = matching[start : start + PAGE_SIZE]
        genre_rows = []
        for item in categories:
            children = [
                {**child, "count": leaves[child["name"]]}
                for child in item.get("children") or []
                if isinstance(child, dict) and isinstance(child.get("name"), str)
            ]
            genre_rows.append(
                {
                    **item,
                    "children": children,
                    "count": leaves[item["name"]] + sum(child["count"] for child in children),
                }
            )
        return {
            "clips": [{k: row[k] for k in ("id", "title", "style", "model")} for row in selected],
            "tracks": {row["id"]: row["track"] for row in selected},
            "genres": {
                **raw_genres,
                "categories": genre_rows,
                "assignments": assignments,
                "placement": placement,
                "pinned": sorted(pins),
            },
            "counts": {
                "archive": len(rows),
                "classified": sum(leaves.values()),
                "orphans": max(0, len(assignments) - sum(leaves.values())),
                "unsorted": totals["unsorted"],
                "blocked": blocked,
            },
            "filter_counts": {k: facets[k] for k in ("total", "pinned", "unsorted", "audio")},
            "category_stats": {
                "tracks": category_stats["tracks"],
                "pinned": category_stats["pinned"],
                "mix": {
                    k: mix[k] for k in ("system1", "llm", "abstained", "manual", "other", "pinned")
                },
            },
            "page": page,
            "pages": pages,
            "total": total,
        }
