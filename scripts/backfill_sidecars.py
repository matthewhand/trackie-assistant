#!/usr/bin/env python3
"""Write ``<stem>.json`` id sidecars for an archive downloaded before they existed.

Local only: no network, no login. Matches each *public* clip in a library JSON
to the files already in the archive using suno-dl's own ``build_filename`` (the
current ``feat_`` form *and* the pre-1.6 ``feat.`` form, plus the in-run
``_N`` collision suffix), then writes the same sidecar a fresh download would.

* Existing sidecars are never overwritten.
* Archive files are never renamed, moved or deleted.
* Clips whose title is shared by several clips (so the file cannot be tied to
  one id) are *ambiguous*: no sidecar is written for them and they are listed.
* Private clips (``is_public`` false) are ignored entirely.

Input is a library listing (list of clip objects with ``id``, ``title`` and
optionally ``is_public``, ``handle``, ``created_at``, ``model_name``,
``major_model_version``, ``duration``, ``tags``) or a ``clips.json`` (same, with
an optional exact ``file`` name per clip).

Usage::

    python scripts/backfill_sidecars.py ARCHIVE_DIR library.json --dry-run
    python scripts/backfill_sidecars.py ARCHIVE_DIR library.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from suno_dl.models import TrackInfo
from suno_dl.naming import build_filename
from suno_dl.sidecar import build_sidecar, saved_files_for, sidecar_path, write_sidecar
from suno_dl.urls import UUID_RE, song_page_url

MEDIA_EXTS = (".m4a", ".mp3", ".wav", ".mp4")


def _norm(text: str) -> str:
    """Accent/punctuation-insensitive key, used only by ``--fuzzy``."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-zA-Z]+", "", text).casefold()


def load_clips(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("clips") or data.get("items") or list(data.values())
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of clips")
    return [c for c in data if isinstance(c, dict) and UUID_RE.fullmatch(str(c.get("id", "")))]


def scan_stems(directory: Path) -> dict[str, list[Path]]:
    """Map media stem -> media files in ``directory`` (top level only)."""
    stems: dict[str, list[Path]] = defaultdict(list)
    for entry in os.scandir(directory):
        if not entry.is_file():
            continue
        lower = entry.name.lower()
        for ext in MEDIA_EXTS:
            if lower.endswith(ext):
                stems[entry.name[: -len(ext)]].append(Path(entry.path))
                break
    return stems


def _names(clip: dict[str, Any], pattern: str, handle: str) -> tuple[str, str]:
    h = handle or str(clip.get("handle") or "")
    kw = {"title": str(clip.get("title") or ""), "handle": h, "clip_id": str(clip["id"])}
    return (
        build_filename(pattern, **kw),
        build_filename(pattern, ma_safe=False, **kw),
    )


def match_clips(
    clips: list[dict[str, Any]],
    stems: dict[str, list[Path]],
    pattern: str,
    handle: str,
    fuzzy: bool = False,
) -> dict[str, Any]:
    """Pair public clips with archive stems. Pure function; touches no files."""
    matched: dict[str, tuple[dict[str, Any], str]] = {}  # stem -> (clip, kind)
    ambiguous: list[dict[str, Any]] = []
    unmatched_clips: list[dict[str, Any]] = []

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for clip in clips:
        groups[_names(clip, pattern, handle)[0]].append(clip)

    for group in groups.values():
        for clip in group:
            new, old = _names(clip, pattern, handle)
            exact = clip.get("file")
            if isinstance(exact, str) and exact:
                stem = exact
                for ext in MEDIA_EXTS:
                    if stem.lower().endswith(ext):
                        stem = stem[: -len(ext)]
                        break
                if stem in stems and stem not in matched:
                    matched[stem] = (clip, "file")
                    continue
            if len(group) == 1:
                hit = [s for s in dict.fromkeys((new, old)) if s in stems and s not in matched]
                if hit:
                    for s in hit:
                        matched[s] = (clip, "name")
                else:
                    unmatched_clips.append(clip)
                continue
            # Several clips share this name: the archive holds ``name``,
            # ``name_2`` ... and which file is which clip is not recorded.
            candidates = [
                s
                for base in dict.fromkeys((new, old))
                for s in [base, *(f"{base}_{n}" for n in range(2, len(group) + 2))]
                if s in stems and s not in matched
            ]
            (ambiguous if candidates else unmatched_clips).append(clip)

    if fuzzy:
        loose: dict[str, list[str]] = defaultdict(list)
        for stem in stems:
            if stem not in matched:
                loose[_norm(stem)].append(stem)
        still: list[dict[str, Any]] = []
        for clip in unmatched_clips:
            key = _norm(_names(clip, pattern, handle)[0])
            rivals = [c for c in unmatched_clips if _norm(_names(c, pattern, handle)[0]) == key]
            if len(loose.get(key, [])) == 1 and len(rivals) == 1:
                matched[loose[key][0]] = (clip, "normalised")
            else:
                still.append(clip)
        unmatched_clips = still

    orphan_stems = sorted(s for s in stems if s not in matched)
    # Stems that belong to an ambiguous group are reported there, not as orphans.
    amb_names = {_names(c, pattern, handle)[0] for c in ambiguous}
    amb_names |= {_names(c, pattern, handle)[1] for c in ambiguous}
    amb_stems = [s for s in orphan_stems if re.sub(r"_\d+$", "", s) in amb_names or s in amb_names]
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "ambiguous_stems": amb_stems,
        "unmatched_clips": unmatched_clips,
        "orphan_stems": [s for s in orphan_stems if s not in amb_stems],
    }


def _track_from_clip(clip: dict[str, Any]) -> TrackInfo:
    duration = clip.get("duration")
    try:
        duration_sec = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    def opt(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    clip_id = str(clip["id"]).lower()
    return TrackInfo(
        id=clip_id,
        title=str(clip.get("title") or ""),
        artist=opt(clip.get("handle")),
        duration_sec=duration_sec,
        page_url=song_page_url(clip_id),
        created_at=opt(clip.get("created_at")),
        model_name=opt(clip.get("model_name")),
        model_version=opt(clip.get("major_model_version") or clip.get("model_version")),
        tags=opt(clip.get("tags") or clip.get("style")),
        is_public=True,
    )


def backfill(
    archive: Path,
    clips: list[dict[str, Any]],
    *,
    pattern: str = "{handle}_{title}",
    handle: str = "",
    dry_run: bool = False,
    fuzzy: bool = False,
) -> dict[str, Any]:
    public = [c for c in clips if c.get("is_public", True) is not False]
    stems = scan_stems(archive)
    result = match_clips(public, stems, pattern, handle, fuzzy)
    written = existing = 0
    for stem, (clip, kind) in sorted(result["matched"].items()):
        target = sidecar_path(archive / stem)
        if target.exists():
            existing += 1
            continue
        if dry_run:
            written += 1
            continue
        files = saved_files_for(archive / stem)
        mtime = min(p.stat().st_mtime for p in stems[stem])
        stamp = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = build_sidecar(
            _track_from_clip(clip),
            handle=handle or str(clip.get("handle") or ""),
            saved_files=files,
            downloaded_at=stamp,
            source="backfill",
        )
        data["match"] = kind
        if write_sidecar(target, data, overwrite=False):
            written += 1
    return {
        "clips_total": len(clips),
        "clips_private_ignored": len(clips) - len(public),
        "clips_public": len(public),
        "media_stems": len(stems),
        "matched_stems": len(result["matched"]),
        "sidecars_written": written,
        "sidecars_already_present": existing,
        "clips_ambiguous": len(result["ambiguous"]),
        "clips_unmatched": len(result["unmatched_clips"]),
        "files_unmatched": len(result["orphan_stems"]),
        "dry_run": dry_run,
        "ambiguous": [{"id": c["id"], "title": c.get("title")} for c in result["ambiguous"]],
        "ambiguous_stems": result["ambiguous_stems"],
        "unmatched_clips": [
            {"id": c["id"], "title": c.get("title")} for c in result["unmatched_clips"]
        ],
        "unmatched_files": result["orphan_stems"],
    }


def _check_writable(directory: Path) -> None:
    probe = directory / ".suno-dl-write-test"
    try:
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(f"{directory} is not writable: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("archive", type=Path, help="directory holding the downloaded tracks")
    ap.add_argument("library", type=Path, help="library.json / clips.json with clip ids + titles")
    ap.add_argument("--pattern", default="{handle}_{title}", help="filename pattern used to sync")
    ap.add_argument(
        "--handle", default="", help="handle used in filenames (default: each clip's handle)"
    )
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument(
        "--fuzzy",
        action="store_true",
        help="also pair leftovers by accent/punctuation-insensitive name (unique pairs only)",
    )
    ap.add_argument("--report", type=Path, help="also write the full report as JSON here")
    args = ap.parse_args(argv)

    archive = args.archive.expanduser()
    if not archive.is_dir():
        raise SystemExit(f"{archive} is not a directory")
    if not args.dry_run:
        _check_writable(archive)
    report = backfill(
        archive,
        load_clips(args.library),
        pattern=args.pattern,
        handle=args.handle,
        dry_run=args.dry_run,
        fuzzy=args.fuzzy,
    )
    verb = "would write" if args.dry_run else "wrote"
    print(
        f"public clips: {report['clips_public']} "
        f"(private ignored: {report['clips_private_ignored']})"
    )
    print(f"media stems in archive: {report['media_stems']}")
    print(f"sidecars {verb}: {report['sidecars_written']}")
    print(f"sidecars already present (kept): {report['sidecars_already_present']}")
    print(f"ambiguous clips (no sidecar): {report['clips_ambiguous']}")
    for item in report["ambiguous"]:
        print(f"  AMBIGUOUS {item['id']} {item['title']!r}")
    for stem in report["ambiguous_stems"]:
        print(f"  AMBIGUOUS-FILE {stem}")
    print(f"public clips with no file: {report['clips_unmatched']}")
    for item in report["unmatched_clips"]:
        print(f"  NO-FILE {item['id']} {item['title']!r}")
    print(f"archive files matching no public clip: {report['files_unmatched']}")
    for stem in report["unmatched_files"]:
        print(f"  UNMATCHED-FILE {stem}")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
