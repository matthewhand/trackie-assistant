# ruff: noqa: UP017 - CT107 host Python is 3.10
"""Read CT107 sidecars and media metadata only; emit a HA media-source index."""

import json
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path("/data/trackie-assistant/library")
records = {}
for sidecar in sorted(ROOT.rglob("*.json")):
    rel = sidecar.relative_to(ROOT)
    if any(p.startswith(".") or p == "__pycache__" for p in rel.parts):
        continue
    if sidecar.is_symlink() or any(p.is_symlink() for p in sidecar.parents if p != ROOT.parent):
        raise RuntimeError("Symlink in library path")
    data = json.loads(sidecar.read_text())
    cid = data.get("id")
    if not cid:
        continue
    if str(uuid.UUID(cid)) != cid:
        raise RuntimeError("Invalid clip identity")
    choices = []
    for name in data.get("saved_files", []):
        if not isinstance(name, str) or Path(name).name != name:
            raise RuntimeError("Invalid saved filename")
        media = sidecar.parent / name
        if media.suffix.lower() not in {".m4a", ".mp3", ".wav"}:
            continue
        if media.is_symlink():
            raise RuntimeError("Symlink media")
        st = media.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 1024:
            raise RuntimeError("Invalid audio file")
        choices.append((media, st))
    if not choices:
        continue
    if cid in records:
        raise RuntimeError("Duplicate clip identity")
    media, st = choices[0]
    records[cid] = {
        "clip": {
            "id": cid,
            "title": data.get("title") or cid,
            "style": data.get("tags") or "",
            "model": data.get("model_name") or "",
            "artist": data.get("artist") or "",
        },
        "track": {
            "audio": "media-source://media_source/local/trackie_ct107/"
            + quote(media.relative_to(ROOT).as_posix(), safe="/"),
            "duration": data.get("duration_sec"),
            "created": data.get("created_at") or data.get("downloaded_at"),
            "created_source": "suno" if data.get("created_at") else "file",
        },
    }
print(
    json.dumps(
        {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "records": records},
        ensure_ascii=False,
    )
)
