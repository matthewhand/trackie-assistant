# Trackie Assistant

Linux preview: add-on **1.6.2-rc2**, package **1.6.2rc2**.

> **Linux preview 1.6.2rc2:** Linux audio sync and previews verify producers from each clip's own canonical page and publish prepared file groups. Existing-file repairs require the reviewed offline migration. This release targets Python 3.13 on Linux; container and HAOS add-on validation covers AMD64. Indexed recordings are reused unchanged. See the release notes for discovery and repair limits.

### Current Linux candidate scope

Supported: new M4A audio downloads, profiles and public playlists, CLI/web/API
sync, cover and lyric sidecars, global clip-UUID reuse, read-only previews and
non-deleting archive caps. Indexed recordings are reused without alteration.

The candidate refuses automatic metadata repair, force replacement, rotation,
video retention, sidecar-free output and non-M4A output. Use the reviewed
offline producer migration for older filenames and artist tags. These gates
remain closed until their separate publication paths pass review.

Sync public Suno profiles straight into your Home Assistant media
library — where [Music Assistant](https://www.music-assistant.io)'s
**Filesystem** provider picks them up for playback on any player.

## Setup

1. **Profiles** (Options): add one or more handles, e.g. `@drinko`.
   Multiple profiles = multiple users' public catalogs; each sync walks
   them all, politely and sequentially. Entries may be bare handles, handles
   starting with `@`, or public `https://suno.com/@handle`, `/playlist/id`,
   and `/song/id` URLs. Remove query strings and fragments before entering
   a URL; commas and whitespace are also unsupported.
2. **Media** (Options): syncs default to **audio + cover images**. Leave
   **download_videos** off (default) for Music Assistant — Retained MP4 videos are
   outside the Linux preview scope. **covers** stays on by default.
   Optional **model_version** (e.g. `v6`) keeps only tracks made with
   that Suno model; leave it empty to sync everything.
3. Enable the add-on and open its web panel (**Trackie Assistant** in the sidebar).
4. In Music Assistant: *Settings → Providers → Filesystem* and point it
   at the `media/suno` folder. Tracks appear under each profile's name.

## Archive directory trust

Keep `/data/archive` and every directory above it under trusted, exclusive
control for the entire time the add-on is running. Do not allow another
process or user to rename or replace those directories or their path
components. Startup checks existing components for symlinks, but the add-on
and its child processes subsequently reopen the path; an active concurrent
replacement can redirect their writes despite the startup check. Stop the
add-on before changing its archive path or storage mounts.

## How it works

Downloads go directly to `/media/suno/<profile>/` on the HA box — no
other machine involved, no LAN copies. Sync on start is off by default. Select sources and preview them in the
web panel before starting a sync. Configure the interval in the add-on options
if you want scheduled runs; the panel shows live progress.

Already have tracks from the CLI on another machine? Copy them into
`/media/suno/<profile>/` once — sync reuses indexed recordings by clip ID. Existing files without ID
sidecars need a separate inventory before their identities can be established.

## Free-space check

The **min_free_mb** option checks free space on the target filesystem before
scheduling each track. Its default is 2048 MiB; set it to 0 to disable the
check. The check is a point-in-time threshold, not a reserved quota: downloads
already underway and other applications can consume space after a check, so
free space can still fall below the configured value.

## Keep a rolling library (caps & rotation)

The sync normally mirrors everything a profile ever published, so the
library only grows. Three Options bound it instead — all default **off**,
so nothing changes until you set one:

| Option | Meaning |
| --- | --- |
| **max_tracks** | Keep only the newest *N* tracks (0 = no cap). |
| **max_disk_mb** | Cap the archive at *N* MiB (0 = no cap). |
| **rotate** | With a cap set, delete the OLDEST tracks after each sync until the archive fits. |

- **rotate off** (default): once the cap is reached, syncs stop downloading
  new tracks. Nothing is ever deleted — a bounded mirror.
- **rotate on**: after each sync the oldest tracks are deleted, together with
  their sidecars (cover images, MP4 videos), until the archive is back under
  the caps. Age is Suno's own `created_at` when known, otherwise the file's
  mtime. A deleted track is not downloaded again by the next sync.

Example: `max_tracks: 20` + `rotate: true` keeps the newest 20 tracks of your
profile, rolling forward as new ones appear.

Deletion is deliberately conservative: it only ever touches files inside
`/media/suno`, never follows or removes through a symlink, logs every single
removal in the add-on log (`ha apps logs local_suno_dl`), and refuses a pass
that would delete the entire library. Preview what it would do from the
terminal add-on:

```console
suno-dl --sync --dry-run --max-tracks 20 --rotate
```

Rotation bookkeeping (track ages, which tracks were pruned) lives in
`/media/suno/.suno-rotation.json`. Deleting it is safe: the tool simply
forgets the recorded ages and starts over from file timestamps.

> These options need a CLI that knows them. The add-on runs a **vendored
> wheel**, so after updating to this add-on version rebuild the wheel from a
> checkout of this repo first (see *Updating after a new release* below);
> until then, leave the three options at their defaults.

## Tags: why tracks showed as `[unknown]`

Every downloaded file is written with embedded metadata — `artist` is the
profile handle (`drinko`), `title` is the full track title. Music Assistant
needs it: without tags it guesses from the filename, which is how tracks
showed up as `[unknown]` and how a title like `Ajani, Nacatl Pariah (feat.
Pyotr Kropotkin)` got cut off after `feat` (it splits a basename at the first
`.`). New files also use `feat_` in the filename so the guess cannot bite
even if the tags are lost.

Tags are written with mutagen if it is installed, otherwise with
`ffmpeg -c copy` — which the add-on image already ships. Either way the audio
is never re-encoded.

### One-shot retag of the existing library

An archive seeded before tagging has no metadata. Fix it in place, from the
terminal add-on (or `ha addons` / `ha cli`), without re-downloading
anything:

```console
# one profile
suno-dl --retag /media/suno/drinko --retag-archive-root /media/suno

# or every profile under /media/suno
suno-dl --retag /media/suno --retag-archive-root /media/suno
```

Safe to re-run: already-tagged files are skipped, nothing is renamed, and no
request is made. Add `--artist drinko` to force the artist tag, or
`--dry-run` to preview. Then rescan in Music Assistant so the new metadata is
picked up.

## Updating after a new release

The add-on runs a **vendored wheel**, so a new `suno-dl` version is not picked
up by a normal update — the image has to be rebuilt from
`/local_apps/suno_dl/`. The add-on version and the CLI version move
independently: add-on **1.6.1** retains the rolling-library options
(`max_tracks` / `max_disk_mb` / `rotate`) and vendors the branded CLI
**1.5.3** wheel. The wheel must be built from the matching approved source;
older wheels with the same package identifier do **not** contain the approved
Trackie Assistant WebUI assets. `suno-dl --version` continues to print the
existing CLI command name and the Python distribution version. The add-on version is deliberately separate from the CLI version. The
defaults are unchanged (no caps, rotate off), so an installation that does not
set them behaves exactly as before.

Preflight first — the rebuild pulls ffmpeg, the wheel's dependencies and a
Chromium build, and it keeps the old image until the new one is complete:

```console
ha host info | grep disk_free          # want 8 GB or more
```

Then, from a checkout of this repo:

1. Build the wheel and vendor it, removing the stale one — the `COPY` glob in
   the Dockerfile matches *every* wheel in the directory, so two of them fail
   the build:

   ```bash
   # Requires an already installed compatible setuptools/wheel build toolchain.
   # Keep the operation offline and leave the installed package untouched.
   mkdir -p dist
   python -m pip wheel --no-index --no-deps --no-build-isolation -w dist .

   old=(addon/suno_dl/trackie_assistant-*.whl)
   new=(dist/trackie_assistant-*.whl)
   test "${#old[@]}" -eq 1 && test "${#new[@]}" -eq 1
   test "$(basename "${old[0]}")" != "$(basename "${new[0]}")"
   # Only after verifying the new wheel's version and contents:
   git rm -- "${old[0]}"
   cp -- "${new[0]}" addon/suno_dl/
   test "$(find addon/suno_dl -maxdepth 1 -name '*.whl' | wc -l)" -eq 1
   ```

2. Copy the add-on context to the box (`config.json`, `Dockerfile`, `run.sh`,
   `DOCS.md`, the wheel) and confirm exactly one wheel is present:

   ```console
   ls -1 /local_apps/suno_dl/*.whl
   ```

3. Rebuild and restart — `rebuild`, not `update`: it re-runs the Dockerfile
   whatever the supervisor thinks the current version is.

   ```console
   ha apps rebuild local_suno_dl
   ha apps restart local_suno_dl
   ha apps logs local_suno_dl
   ```

4. Confirm the new CLI is the one running (Terminal add-on):

   ```console
   suno-dl --version        # 1.5.3
   suno-dl --help | grep retag
   ```

   The branded web panel shows the same CLI version alongside its
   **Trackie Assistant** heading. Verify the add-on sidebar label is
   Trackie Assistant and the add-on remains `local_suno_dl`.

5. Migrate the existing library, then let Music Assistant re-index it:

   ```console
   suno-dl --retag /media/suno --dry-run
   suno-dl --retag /media/suno --retag-archive-root /media/suno
   ```

6. **Rescan in Music Assistant** (Settings → Providers → Filesystem →
   Rescan). Metadata and filenames are read at index time, so a rescan is
   what makes the change visible in the UI.

## Blocklist

A track that must never be fetched (e.g. Suno permanently 403s it) goes
in `/data/archive/.blocked` — one clip ID per line. It is checked before
any request. Create it via the terminal add-on:

```console
echo 7c68eb8d-4bad-43d5-8d37-8be1fd48fb58 >> /addon_configs/…/…
```

(or ask the assistant to manage it for you).

## Privacy & politeness

- Only **public** sources are read. No session cookie, login or private
  decryption is used by the supported Linux sync path.
- Human-paced defaults: sequential downloads, jittered delays, one run
  at a time. No parallel hammering.
- No secrets leave the add-on; the web panel never displays them.


## Container security baseline

The local candidate uses digest-pinnable Wolfi, Python 3.13 and maintained
FFmpeg 9 packages. Application dependencies live in /opt/venv. Chromium's
runtime libraries come from the signed APK repository; Playwright installs
its matched headless browser in /opt/playwright. The obsolete recorder helper
is removed because Trackie does not record browser sessions.
