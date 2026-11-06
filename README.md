# Trackie Assistant

![Trackie Assistant banner featuring Trackie Dacks, the teal tracksuit-pants mascot wearing headphones](docs/trackie-assistant-banner.png)

> **Linux preview 1.6.2rc1:** Linux audio sync and previews verify producers from each clip's own canonical page and publish prepared file groups. Existing-file repairs require the reviewed offline migration. This release targets Python 3.13 on Linux; container and HAOS add-on validation covers AMD64. Indexed recordings are reused unchanged. See the release notes for discovery and repair limits.

### Current Linux candidate scope

Supported: new M4A audio downloads, profiles and public playlists, CLI/web/API
sync, cover and lyric sidecars, global clip-UUID reuse, read-only previews and
non-deleting archive caps. Indexed recordings are reused without alteration.

The candidate refuses automatic metadata repair, force replacement, rotation,
video retention, sidecar-free output and non-M4A output. Use the reviewed
offline producer migration for older filenames and artist tags. These gates
remain closed until their separate publication paths pass review.

[![CI](https://github.com/matthewhand/trackie-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/matthewhand/trackie-assistant/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

**Meet Trackie Dacks.** Tracksuit pants, headphones and a musical-note
drawstring: the mascot of **Trackie Assistant**. Keep the public music you
own or have permission to archive, find it again and manage it locally.
The README banner features Trackie Dacks; the original approved logo is preserved
in `docs/trackie-assistant-logo.webp`.

## What it does

- **Keep your tracks:** discover public profiles, playlists and individual
  songs, retain cover art, and use clip-ID sidecars to resume even when
  song titles change.
- **Save audio and covers:** use explicitly published audio or a temporary
  published MP4 source. Audio is extracted without re-encoding; temporary video
  is discarded. Home Assistant ingress deliberately does not retain MP4.
- **Stay in control:** use the CLI or local WebUI, preview with
  `--dry-run`, and set library size/count caps. A nonrotating disk
  cap is conservative, not an absolute filesystem quota.
- **Public sources only:** no account sign-in, cookie collection or
  private media decryption.

> **Current Linux candidate:** new downloads are M4A audio with verified producer
> tags and optional cover art. Retaining video and other output formats is
> refused. Linux, Python 3.13, FFmpeg/ffprobe and Mutagen are required for writes.
> Windows and macOS archive writes are outside this release's supported scope.

The Sync panel shows the current batch's queued track count, an indicative
saved-size range and free space on the configured destination filesystem.
The selected-playlist HAOS monitor shows the whole selected job's queue.
Size estimates assume 128–256 kbps audio plus artwork; missing durations use
four minutes per track. These are estimates, not upper bounds. Temporary
video transfers and working space are additional and network size is unknown.
Capacity telemetry is available in new runs; older workers report it unavailable.

## Compatibility with existing installations

Trackie Assistant is the **user-facing product name** and the Python
distribution is **`trackie-assistant`**. The existing CLI commands
`suno-dl`, `suno-webui` and `suno-recommend` remain available.
The Python import `suno_dl` and environment variables keep their names.
The public source now lives in the dedicated Trackie Assistant repository. The add-on slug
remains `suno_dl`. Home Assistant assigns a repository-specific ID; adding
this repository creates a separate install from an existing local add-on.
Existing installations and files are not migrated automatically.

The former distribution `suno-download-cli` and `trackie-assistant`
share the same Python module and commands. **Do not install both in the same
environment.** For an existing standalone installation, stop its CLI/web/API
processes, uninstall the old distribution, then install the reviewed
`trackie_assistant-*.whl` release wheel in that environment. Installing
the package does not move, rename or delete your music. Keep your existing
configuration and restart the app only after checking its version.

This is a Linux preview release. PyPI publication is not enabled; install the
reviewed release wheel or from source. From a checkout, `pip install -e .` installs
the new distribution name. HAOS rebuilds install the bundled wheel into a
fresh add-on image. Rebuilding within the same HA repository keeps its add-on
ID; switching repositories changes the repository prefix.

## Home Assistant

[Add the Trackie Assistant repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmatthewhand%2Ftrackie-assistant),
then select **Trackie Assistant** in the app store. Enter your own profiles or
playlist URLs; the preview starts with no sources selected and **Sync on start**
off. Preview your sources before downloading. The AMD64 preview builds
locally from the bundled reviewed wheel. See [add-on setup](addon/suno_dl/DOCS.md)
and the [preview release notes](docs/releases/v1.6.2rc1.md).

## How it works

Suno's anonymous public feed can list profile tracks without a browser,
while song and playlist details may arrive in client-rendered page data.

1. **Discovery (profile URLs)** — the anonymous HTTP feed is attempted
   first. If it fails or returns no tracks, browser-enabled discovery
   falls back to the **optional Playwright** browser to scroll public
   `/song/<uuid>` links. Direct playlist/song URLs use `httpx` and
   BeautifulSoup to parse available page data; they never need Chromium.
2. **Producer and media verification** — each clip's own canonical page
   supplies its producer and explicitly published media. Encoded media is skipped;
   guessed CDN URLs and private decryption are not used by the Linux engine.
3. **Download** — media is validated in private staging. A published MP4 can be
   used temporarily for verified audio extraction without re-encoding; it is
   discarded before publication. New file groups are published under the common
   library lock with recovery intents. Existing recordings are reused unchanged.
4. **Sidecar** — next to each saved track, a small `<same name>.json` records the
   permanent Suno clip id and the original title (see [Id sidecars](#id-sidecars)).
   It is public metadata only, and it is what lets a re-run recognise a track it
   already has even when the filename would differ.

## Legal / Terms

This tool downloads **any public** track it is pointed at; it cannot know who owns
a track. Intended use, and your responsibility:

- Archive content you own or are otherwise permitted to download.
- Do **not** use it to obtain unlisted, deleted, or access-restricted content.
- It never logs in or uses cookies or session tokens. Playlist membership has one narrow
  exception to the no-private-API rule: anonymous GET requests only to the exact HTTPS
  endpoint `https://studio-api.prod.suno.com/api/playlist/{uuid}/?page=N`. No other
  API path or method is used; redirects, cookies, and authorization are refused.
- Respect Suno's Terms of Service and applicable copyright law. You are
  responsible for the tracks you download.
- **Published media only:** encoded or inaccessible audio is not decrypted.
  The default permits explicitly published MP4 as a temporary audio source;
  only verified M4A audio and requested artwork are kept. Use
  `--no-extract-audio` to disable this fallback. Provider refusals are not
  permission to invent URLs, collect credentials or bypass access controls.
- Be polite: the defaults are already human-paced (sequential downloads, jittered
  1.5 s per-host gap). If you raise `--concurrency`, keep it low against the same
  profile.

If Suno changes its site structure or CDN conventions, this tool may stop working
until it is updated.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Found 0 public track(s)` | The profile may be private/renamed, or Suno changed its markup. Try `--url` with a direct song URL. |
| Everything fails with 403 | Signed CDN URLs expire; the tool resolves them fresh, so re-run. Persistent 403s suggest an upstream change. |
| `playwright` / browser errors | The profile feed does not need Chromium. For the browser fallback, install the optional `.[browser]` extra and run `python -m playwright install chromium`; `--no-browser` disables fallback. |
| Profile stops at ~20 tracks | Suno limits anonymous views of long profiles; run again, or use the profile's song URLs directly. |
| No audio file created | Check the summary line: `failed=` counts tracks with no accessible public media. |

Set `-v` for debug logs. Debug output redacts signed URLs, so it is safe to paste.

## Prerequisites

- Python **3.13+**
- Optional for the profile browser fallback: the `browser` extra and Chromium
  (`python -m playwright install chromium`). HTTP/feed-only use needs neither.

**Strict browser fallback:** `--browser` is on by default. If profile feed discovery
fails or returns no tracks and the `browser` extra is not installed, the CLI exits
with installation instructions rather than silently claiming a complete archive.
Use `--no-browser` to deliberately accept HTTP-only discovery and possible gaps.

## Setup

```bash
# macOS / Linux
git clone https://github.com/matthewhand/trackie-assistant.git
cd trackie-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[tags]"            # verified Linux audio writes require Mutagen
# Optional profile browser fallback:
# pip install -e ".[browser]"
# python -m playwright install chromium
# Development/test dependencies: pip install -e ".[dev]"

# Archive writes in this candidate are supported on Linux only.

# Configuration
cp .env.example .env
# edit .env — set SUNO_DEFAULT_HANDLE to your handle (no @)
```

`.env` is in `.gitignore` and must never be committed.

The optional `suno-recommend` decision engine is **not configured by default**.
Without `SUNO_JEV_URL` or `--jev-url`, recommendations use a deterministic
heuristic (no request to a JEV/SystemOne service). To opt in, set
`SUNO_JEV_URL` in your private `.env` to an endpoint you control, for example
`http://127.0.0.1:8903/v1/evaluate`. The web dashboard reports which mode is
configured. Fetching public Suno profile information still requires internet access.

## Usage

```bash
# Default: uses SUNO_DEFAULT_HANDLE / SUNO_DEFAULT_URL from .env
suno-dl

# Explicit URLs
suno-dl --url https://suno.com/@drinko
suno-dl --url https://suno.com/playlist/abc123
suno-dl --url https://suno.com/song/xyz789 --limit 5

# Testing without writing files
suno-dl --url https://suno.com/@drinko --dry-run --limit 3

# Custom tuning (defaults are already sequential + paced; tune up only if needed)
suno-dl --concurrency 3 --rate-limit 0.5 --output-dir ./music --pattern "{handle}_{title}"

# Write artist/title tags into an archive that predates them (local only,
# no re-download, safe to re-run)
suno-dl --retag ./music --retag-archive-root ./music
```

### Options

| Flag | Meaning |
| --- | --- |
| `--url` | Explicit public Suno URL (profile, playlist, or song). |
| `--limit N` | Cap the number of downloads. |
| `--pattern` | Filename template; placeholders `{handle}`, `{title}`, `{id}`. Default: `{handle}_{title}`. |
| `--concurrency` | Max parallel downloads. `1..20`, default from `.env`. |
| `--output-dir` | Destination folder. Default `./downloads` (gitignored). |
| `--format` | Prefer a media format (`m4a`, `mp4`, `mp3`, `wav`); others remain fallbacks. |
| `--rate-limit SEC` | Minimum seconds between requests to the same host, jittered ×[0.75–1.5]. Default `1.5`; `0` disables pacing. |
| `--videos` / `--no-videos` | Download MP4 video tracks. **Default: off** (`SUNO_DOWNLOAD_VIDEOS=0`). |
| `--covers` / `--no-covers` | Fetch each track's cover JPEG/PNG. **Default: on** (`SUNO_COVERS=1`). The Linux additions engine embeds validated JPEG/PNG artwork in the M4A and also saves the image alongside it, so Music Assistant can read the embedded cover. |
| `--max-tracks N` | Library cap: keep only the newest **N** tracks in the archive. **Default: off** (`SUNO_MAX_TRACKS`). |
| `--max-disk-mb N` | Library cap on the archive size in MiB. **Default: off** (`SUNO_MAX_DISK_MB`). |
| `--rotate` / `--no-rotate` | With a cap set, delete the **oldest** tracks after the sync until the archive fits. **Default: off** (`SUNO_ROTATE`) — downloads stop at the cap and nothing is ever deleted. |
| `--sidecar` / `--no-sidecar` | Write a `<same name>.json` id/metadata sidecar beside each track, and skip tracks whose clip id a sidecar already records. **Default: on** (`SUNO_SIDECAR=1`). See [Id sidecars](#id-sidecars). |
| `--playlists` / `--no-playlists` | Also archive the ordered membership of every **explicitly public** playlist the profile advertises. **Default: off** (`SUNO_PLAYLISTS=0`); resumable across runs, and private/hidden/trashed listings are never selected. A playlist whose page exposes no song ids is reported *unresolved*, never counted as empty. |
| `--audio-only` | Audio-only output; use `--no-extract-audio` to also disable temporary video sources. |
| `--flat` | Write into `--output-dir` directly instead of the default per-source subfolder (`<output-dir>/<handle-or-id>/`). |
| `--extract-audio` | Allow temporary published MP4 sources for audio/artwork. Enabled by default; videos are discarded after verified extraction without audio re-encoding. |
| `--keep-video` | With `--videos --extract-audio`, keep MP4 next to its tagged M4A. Only the non-ingress WebUI enables this opt-in; Home Assistant ingress and scheduled sync retain audio-only extraction. |

With `--keep-video --max-disk-mb` and rotation off, a video transfer reserves
twice the incoming MP4 bytes plus **64 KiB** for the accompanying M4A and
metadata. A pre-existing M4A requires only MP4 bytes plus overhead; an old
MP4 is remuxed only when budget remains. **Cover artwork is optional:** covers
that exceed the remaining budget are skipped instead of pushing the archive
over the cap. Under a cap, downloads are serialized; separate CLI processes
coordinate through the retained `.suno-sync.lock` file in the archive root.
That file is intentionally not deleted, and a competing sync waits at most
30 seconds before reporting a busy archive. Do not remove it while syncing.

The nonrotating cap is **not an absolute filesystem quota**: another program
that does not cooperate with this lock can still write into the archive,
and an unusually expanding remux or metadata can exceed the estimates.
With `--rotate`, the existing safeguard deliberately refuses to delete
every track in an oversized archive; this can leave the archive over cap.
The `--max-tracks` limit counts distinct tracks: adding MP4 to an existing
M4A does not spend an additional track slot.

| `--retag DIR` | Write `artist`/`title` tags into an existing archive and exit. No requests, no renames; already-tagged files are skipped. See [docs/usage.md](docs/usage.md#retagging-an-existing-archive). |
| `--retag-archive-root DIR` | Authoritative sync archive root for mutating retag operations; the retag target must be inside it. |
| `--artist HANDLE` | Artist for files selected by `--retag`; files with a title tag are skipped. Default is the handle in each filename. |
| `--sync` | Sync every watch-list source (`SUNO_WATCH=handle1,handle2` in `.env` and/or repeatable `--watch <entry>`); skips already-downloaded tracks, one dead source never blocks the rest. |
| `--dry-run` | Print the full target path for each track; write nothing. |
| `--browser/--no-browser` | Toggle Playwright discovery for profiles (default on). |
| `--version` | Print the version and exit. |
| `--verbose` / `-v` | DEBUG logging (signed URLs are redacted). |

### Rolling library: `--max-tracks` / `--max-disk-mb` / `--rotate`

`--limit` caps **one run**; these cap the **archive** (all sources combined)
and stay in force across runs. Both caps default to off, so the default
behaviour is unchanged.

```bash
# Keep the newest 20 tracks; once the cap is reached, stop downloading.
# Nothing is ever deleted.
suno-dl --sync --max-tracks 20

# Same, but delete the oldest tracks (and their covers/videos) after the sync
# until the archive fits — a rolling library.
suno-dl --sync --max-tracks 20 --rotate

# At most 1 GB on disk, oldest first
suno-dl --sync --max-disk-mb 1024 --rotate

# Preview a pruning pass without deleting anything
suno-dl --sync --max-tracks 20 --rotate --dry-run
```

With `--rotate` on, tracks are deleted oldest first — by Suno's `created_at`
when known, else file mtime — together with every sidecar of the same track
(`.m4a`, `.mp4`, `.cover.jpg`, …). A pruned track is not re-downloaded by a
later sync. With `--rotate` off a cap only stops new downloads.

Rotation checks archive containment and symlinks, logs removals, and refuses
to delete the *whole* archive; a failed or interrupted prune can remain over
cap. These checks are not atomic against a hostile concurrent writer: use
rotation only on a trusted filesystem with exclusive, cooperating writers.
Never automatically purge orphan `*.part` staging files; inventory them
without deletion and obtain separate authorization for supervised cleanup.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean run (everything downloaded or already present). |
| `1` | Partial failure (at least one track failed). |
| `2` | No tracks found / usage error. |

### Output example

```console
$ suno-dl --url https://suno.com/@drinko --limit 3 --output-dir ./music
Found 3 public track(s).
Downloading: 100%|██████████| 3/3 [00:02<00:00,  1.4track/s]
Summary: total=3 downloaded=3 existing=0 tagged=3 sidecars=3 failed=0
Saved to /home/you/music
```

Resulting files — each carrying the handle as `artist` and the full title as
`title`, so a music server (Music Assistant, Plex, …) shows them correctly
instead of `[unknown]` or a title truncated at the first period:

```
music/
├── drinko_Absolutely Certain That I Might Be Odd.m4a
├── drinko_Agora on the Veranda.m4a
└── drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).m4a
```

A mid-name period is written as `_` in the filename (the real `feat.` lives in
the tag): tag-guessing players split a basename at the first `.` and would
otherwise show the title as `drinko_Ajani, Nacatl Pariah (feat`.

### Id sidecars

Filenames are lossy: sanitising drops characters such as `: / ?`, accents and
non-breaking spaces, `feat.` becomes `feat_`, and two clips with the same title end up
as `Name.m4a` and `Name_2.m4a` with nothing recording which is which. Every Suno clip
has a permanent id (the UUID in `suno.com/song/<id>`), so when suno-dl saves a track
it also writes a small JSON file with the **same name** next to it, and the permanent
clip id is the key to match files to clips:

```
music/drinko/
├── drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).m4a
├── drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).cover.jpg
└── drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).json
```

```json
{
  "schema": 1,
  "id": "7a0f1acc-3613-4831-80c3-a35cc5cd8991",
  "url": "https://suno.com/song/7a0f1acc-3613-4831-80c3-a35cc5cd8991",
  "title": "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)",
  "handle": "drinko",
  "created_at": "2026-10-09T01:02:03.000Z",
  "model_name": "chirp-fenix",
  "model_version": "v5",
  "duration_sec": 213,
  "tags": "style text as Suno shows it",
  "is_public": true,
  "saved_files": ["drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).m4a"],
  "suno_dl_version": "1.5.2",
  "downloaded_at": "2026-10-09T02:00:00Z",
  "source": "download"
}
```

- **Original titles survive.** `title` is exactly what Suno shows, even when the
  filename had characters replaced or removed. (The embedded `artist`/`title` tags
  carry it too, but only for formats that can hold tags; the sidecar works for all.)
- **Naming.** `<stem>.json`, where `<stem>` is the media filename without its
  extension, so audio, video (`.mp4`) and cover share one sidecar. `saved_files`
  lists the files that exist for that stem. Unknown values are `null`.
- **Public metadata only.** No cookies, tokens, signed URLs or other secrets are ever
  written. `source` is `download` for a fresh save, `existing` when a track already on
  disk got its sidecar on a later sync, and `backfill` when written by the script
  below.
- **Skip by id.** Before downloading, suno-dl reads the sidecars in the output folder.
  A track whose id is recorded there (and whose media file is still on disk) is
  skipped even if the filename it would get today is different, e.g. after a title
  edit or a sanitiser change. Delete the media file and the track is fetched again.
  An existing sidecar is never overwritten when a track is merely skipped.
- **Turning it off.** `--no-sidecar` (or `SUNO_SIDECAR=0`) writes no sidecars and
  skips by filename only, as before.
- **Backfilling an existing archive.** Local only (no requests, no login). It matches
  public clips from a library/clips JSON to files using suno-dl's own filename rules
  (both the current `feat_` and the old `feat.` form, plus the `_2` collision suffix),
  never overwrites a sidecar and never renames, moves or deletes media:

  ```bash
  python scripts/backfill_sidecars.py ./music/drinko library.json --dry-run
  python scripts/backfill_sidecars.py ./music/drinko library.json
  ```

  Private clips (`is_public` false) are ignored. Clips that share a title with another
  clip cannot be told apart from the filename alone, so they are reported as
  **ambiguous** and get no sidecar; files that match no public clip are listed as
  unmatched. `--fuzzy` also pairs leftovers by accent/punctuation-insensitive name
  (unique pairs only), and `--report FILE` saves the full report as JSON.

## Documentation

- [Architecture](docs/architecture.md) — module map, data flow, design decisions
- [Usage & recipes](docs/usage.md) — patterns, rate limiting, scripting
- [Troubleshooting](docs/troubleshooting.md) — common failures and fixes
- [Release verification](docs/release-verification.md) — runbook for verifying releases when the Actions budget is exhausted
- [Handoff note](docs/handoff.md) — what shipped, how it was verified, what was deliberately left undone

## Tests

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy src
```

Tests use static and captured HTML fixtures plus httpx `MockTransport`; the suite
makes no network access. CI is reproduced locally with `make ci`
(see [scripts/README.md](scripts/README.md)) — required because GitHub Actions is
unavailable on this account.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).
Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).

## Known limitations

- **Profile pagination depends on ScrollArea render.** The built-in browser scroll loop
  handles this; if Suno virtualises the list, the profile's song counter is used as the
  stop condition and the run exits when no new anchors appear for several iterations.
- **Playlists** are static HTML (server-rendered) and work without a browser.
- **Songs** resolve their media URL from the embedded RSC payload; tracks with no
  public media object return HTTP 403 and are logged + skipped.
- **Signed CloudFront URLs** expire (the signature is time-limited). The URL is
  resolved fresh at download time, so runs work, but a URL captured hours earlier
  will 403 — re-run instead of reusing saved URLs.
- **WAV** is not supported in this build.
