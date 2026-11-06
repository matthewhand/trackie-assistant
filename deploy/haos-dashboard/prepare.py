"""Prepare HA dashboard assets from read-only CT107 inventory; does not deploy."""

import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--ct-host", required=True, help="SSH host for the container hypervisor")
parser.add_argument("--ha-host", required=True, help="SSH host for Home Assistant")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
here = Path(__file__).resolve().parent


def ssh(host, command, source=None):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


index = json.loads(
    ssh(args.ct_host, "pct exec 107 -- python3", (here / "export_ct107.py").read_text())
)
for name in (
    "suno-genre-card.js",
    "suno-genre-core.js",
    "clips.json",
    "genres.json",
    "remote.json",
):
    text = ssh(args.ha_host, "cat /config/www/suno-genres/" + name)
    (args.out / ("before-" + name)).write_text(text)
old = {c["id"]: c for c in json.loads((args.out / "before-clips.json").read_text())}
clips, tracks, lyrics = [], {}, {}
for cid, record in index["records"].items():
    clip = record["clip"]
    prior = old.get(cid, {})
    clip["style"] = clip["style"] or prior.get("style") or ""
    clip["model"] = clip["model"] or prior.get("model") or ""
    if not all(isinstance(clip[k], str) for k in ("id", "title", "style", "model")):
        raise ValueError("Non-text clip metadata")
    clips.append(clip)
    tracks[cid] = record["track"]
    if prior.get("lyrics"):
        lyrics[cid] = prior["lyrics"]
genres = json.loads((args.out / "before-genres.json").read_text())
genres["assignments"] = {k: v for k, v in genres.get("assignments", {}).items() if k in tracks}
snapshot = {
    "schema": 1,
    "generated_at": index["generated_at"],
    "clips": clips,
    "tracks": tracks,
    "lyrics": lyrics,
    "genres": genres,
}
(args.out / "ct107-library.json").write_text(json.dumps(snapshot, ensure_ascii=False))
base = (args.out / "before-suno-genre-card.js").read_text()
marker = chr(10) + 'import { installHaLibraryBridge } from "./trackie-ha-bridge.js'
# Refreshing the snapshot must not install the bridge twice.
base = base.split(marker, 1)[0]
base = base.replace(
    "Read-only here: move and pin songs on the Home Assistant card.",
    "CT107 library view: genre changes are disabled here.",
)
bridge = (
    chr(10)
    + 'import { installHaLibraryBridge } from "./trackie-ha-bridge.js?v=20261106-1";'
    + chr(10)
    + "installHaLibraryBridge(SunoGenreCard);"
    + chr(10)
)
(args.out / "suno-genre-card.js").write_text(base + bridge)
(args.out / "trackie-ha-bridge.js").write_text((here / "trackie-ha-bridge.js").read_text())
print(json.dumps({"recordings": len(clips), "generated_at": index["generated_at"]}))
