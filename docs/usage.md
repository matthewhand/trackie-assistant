# Usage & recipes

> **Linux preview 1.6.2rc16:** Linux audio sync and previews verify producers from each clip's own canonical page and publish prepared file groups. Existing-file repairs require the reviewed offline migration. This release targets Python 3.13 on Linux; container and HAOS add-on validation covers AMD64. Indexed recordings are reused unchanged. See the release notes for discovery and repair limits.

### Current Linux candidate scope

Supported: new M4A audio downloads, profiles and public playlists, CLI/web/API
sync, cover and lyric sidecars, global clip-UUID reuse, read-only previews and
non-deleting archive caps. Indexed recordings are reused without alteration.

The candidate refuses automatic metadata repair, force replacement, rotation,
video retention, sidecar-free output and non-M4A output. Use the reviewed
offline producer migration for older filenames and artist tags. These gates
remain closed until their separate publication paths pass review.

## Install

```bash
pip install -e .             # lean HTTP/feed install from a clone
# For optional profile browser fallback:
# pip install -e ".[browser]"
# python -m playwright install chromium
# For development and tests: pip install -e ".[dev]"
```

## Basic

```bash
# Profile (tries HTTP feed first; browser fallback requires optional [browser] extra)
suno-dl --url https://suno.com/@yourhandle
# Feed-only profile discovery without optional Chromium:
# suno-dl --url https://suno.com/@yourhandle --no-browser

# A single song
suno-dl --url https://suno.com/song/<uuid>

# A playlist
suno-dl --url https://suno.com/playlist/<id>
```

## Recipes

### Archive only your newest 20 tracks

```bash
suno-dl --url https://suno.com/@yourhandle --limit 20
```

That caps a single run. To cap the *archive* — every run, every source — see
[Keep a rolling library](#keep-a-rolling-library-caps--rotation) below.

### Include public playlists, resumably

A profile feed lists clips *and* playlists; the playlists can hold recordings
the profile no longer shows. `--playlists` opts into them:

```bash
suno-dl --sync --playlists
```

- Only **explicitly public** listings are selected. A playlist flagged
  private, unlisted, hidden, trashed or deleted is skipped and counted
  (`skipped_not_public`), including nested private/hidden visibility markers.
  Unmarked anonymous-feed entries remain discoverable, but membership still
  requires public validation. Conflicting page totals mark discovery truncated.
- Membership is read from the public page first. HTML membership is only
  complete when an explicit public profile-feed count matches its unique song
  IDs and does not conflict with any page count. Unanchored page text alone
  is not proof of completeness; an independently verified public API response
  takes precedence over that text. Over-cap HTML is unresolved, never
  truncated and checkpointed as complete. If counts remain ambiguous,
  the sole API exception is an
  anonymous GET to `https://studio-api.prod.suno.com/api/playlist/{uuid}/?page=N`.
  This exact HTTPS host/path is allow-listed for membership only: GET only,
  no cookies or authorization, no redirects, a fresh client per page, a 4 MiB
  response cap, at most 100 pages and 5,000 tracks. Playlist identity and
  explicit public/not-hidden/not-trashed flags are checked on every page and
  clip; the API total must agree with the profile-feed count when available,
  and every returned song UUID must be valid and unique. Any mismatch, partial page or ambiguity remains
  unresolved. No other API path is permitted.
- Membership is written to
  `.library/membership.json` as ordered UUID references, and never duplicated:
  the global library from #158 stores one recording per UUID, so a track in
  three playlists is still one file.
- A page that exposes no song ids is **unresolved**, not empty. That is the
  usual outcome for older records, and the run names them:

  ```text
  Playlists: found=16 expanded=9 skipped=0 unresolved=7 tracks=264
    unresolved playlist 'old mix': no public song ids in the page
  ```

  An unresolved playlist is retried next run; it is never recorded as complete.
- Verified membership is checkpointed in `.library/playlist-checkpoint.json`,
  but that checkpoint does not mean the tracks were downloaded. Each run
  rechecks playlist membership: a previous download may have been interrupted,
  and the public feed has no reliable revision marker for same-count changes.
  Existing recordings are reused by UUID. `--dry-run` writes neither checkpoint
  nor membership and cannot make a later real run skip work.
- Work is bounded end to end: 20 feed pages, 200 playlists, 5000 tracks per
  playlist, the 12 MiB response cap and the usual jittered per-host pacing, one
  playlist at a time.

Pacing, the response cap and the blocklist all apply as usual; rotation stays
whatever you configured (off by default).

### Keep a rolling library (caps & rotation)

Three options bound the archive instead of one run (`--limit` still caps the
run itself and is combined with these). All default to off:

| Option | Meaning |
| --- | --- |
| `--max-tracks N` / `SUNO_MAX_TRACKS` | Keep only the newest N tracks. |
| `--max-disk-mb N` / `SUNO_MAX_DISK_MB` | Cap the archive at N MiB. |
| `--rotate` / `--no-rotate` / `SUNO_ROTATE` | Delete the oldest tracks after a sync, until the archive fits. |

```bash
# Bounded mirror: stop downloading at 20 tracks, delete nothing.
suno-dl --sync --max-tracks 20

# Rolling library: newest 20 tracks, oldest pruned after every sync.
suno-dl --sync --max-tracks 20 --rotate

# Same idea bounded by disk instead of count.
suno-dl --sync --max-disk-mb 1024 --rotate
```

With `--rotate`, pruning *attempts* to meet both caps, but can leave the
archive over cap if deleting every track would be required or removal fails.
Without rotation, downloads stop at the configured cap and no rotation
deletion occurs; neither mode is an absolute filesystem quota. Sync only
considers the newest `--max-tracks` tracks, so a rotation is not re-downloaded
on the next run (`pruned_skipped=N` in the summary counts them).

Age is Suno's `created_at` for the track when the payload carried it (kept in
`.suno-rotation.json` next to the archive), otherwise the file's mtime.
Sidecars go with their track: a pruned `Song.m4a` also removes `Song.mp4`,
`Song.cover.jpg`, and any other file of the same stem.

The destructive path checks archive containment and symlinks, logs removals,
honours `--dry-run`, and refuses a pass that would delete the entire archive.
These pathname checks are **not atomic** against a hostile concurrent writer;
rotation requires a trusted archive and exclusive, cooperating writers.
Interrupted or partially failed pruning may require supervised recovery. Remove
`.suno-rotation.json` to forget which tracks were pruned (they will simply be
downloaded again).

### Set-and-forget via `.env`

```
SUNO_DEFAULT_HANDLE=yourhandle
SUNO_OUTPUT_DIR=~/Music/suno
SUNO_CONCURRENCY=3
SUNO_RATE_LIMIT_SEC=0.5
```

Then simply:

```bash
suno-dl
```

### Sync a whole watch list

Keep several profiles fresh with one command. Set the list once:

```
SUNO_WATCH=handleA,handleB,@handleC
```

or pass entries on the command line (bare handles or full suno.com URLs,
repeatable):

```bash
suno-dl --sync --watch handleA --watch handleB
suno-dl --sync --extract-audio   # uses SUNO_WATCH from .env

### Never download certain tracks (`.blocked`)

Some tracks are permanently unavailable to anonymous downloads — Suno answers
HTTP 403 for those specific objects while every other track in the same run
fetches fine. Repeatedly retrying a blocked object is pointless at best and
looks like automation pressure at worst, so the tool can skip them by
explicit instruction.

Create a `.blocked` file next to your `.env` (one clip UUID per line, `#`
comments; `SUNO_BLOCKED_FILE` overrides the path):

```
# Spring Has No Alibi — Suno 403s this object for anonymous fetches.
# Download manually via Suno's UI instead.
7c68eb8d-4bad-43d5-8d37-8be1fd48fb58
```

Blocked tracks are skipped before any request is made (dry-run included) and
reported separately: `blocked=1` appears in the summary, so a zero-failure
run with `blocked=1` means "everything downloaded except the one we agreed
not to touch". The clip ID is on the track's suno.com page URL
(`suno.com/song/<id>`).
```

Each source syncs into its own subfolder, already-downloaded tracks are
skipped, and one unreachable source never blocks the rest (failed sources
give exit code 1). Per-source lines and a combined summary are printed.

### Custom filenames

```bash
# "Artist - Title"
suno-dl --url https://suno.com/@h --pattern "{handle} - {title}"

# Stable id-first naming (avoids collisions across renames)
suno-dl --url https://suno.com/@h --pattern "{id} - {title}"
```

Available placeholders: `{handle}`, `{title}`, `{id}`.

### Prefer a specific container

```bash
suno-dl --url https://suno.com/@h --format m4a
```

The preferred format is tried first; other formats stay as fallbacks, so this
never causes a download to fail that would otherwise succeed.

### Be gentle on the CDN

Politeness is the default: downloads run **sequentially** (`--concurrency 1`) with a
**jittered** 1.5 s minimum gap between requests to the same host, so the timing does
not look automated.

```bash
# defaults are already human-paced; to go faster:
suno-dl --url https://suno.com/@h --concurrency 3 --rate-limit 0.5
# to disable pacing entirely (fastest, least polite):
suno-dl --url https://suno.com/@h --rate-limit 0
SUNO_RATE_LIMIT_SEC=0 suno-dl --url https://suno.com/@h
```

`--rate-limit` / `SUNO_RATE_LIMIT_SEC` enforce the minimum per-host gap (each actual
delay is randomly varied within [0.75x, 1.5x] of it); `--concurrency` controls how
many tracks download in parallel.

### Save disk space

> The Linux candidate uses explicitly published, unencoded audio or a
> published MP4 as a temporary source for lossless audio extraction. It does
> not decrypt media or invent CDN URLs. Video retention is currently refused;
> saved output is M4A audio and requested artwork. To disable temporary video
> sources, pass `--no-extract-audio`. Linux, FFmpeg/ffprobe and Mutagen are
> required for archive writes.

```bash
# Defaults: audio + covers, videos off (same as --no-videos --covers)
suno-dl --url https://suno.com/@yourhandle

# Disable temporary MP4 audio-source fallback
suno-dl --url https://suno.com/@yourhandle --no-extract-audio

# Skip cover art
suno-dl --url https://suno.com/@yourhandle --no-covers

# Optional Pillow-based cover recompression
pip install -e ".[image]"

# Per-source subfolders (default): each profile/playlist/song gets its own
# directory under --output-dir, e.g. downloads/@drinko/...
#   suno-dl --url https://suno.com/@drinko  ->  <output-dir>/drinko/*.m4a (or .mp4 with --videos)
# Use --flat for the old single-directory layout.

# Extract audio-only M4A after download (lossless ffmpeg remux, no re-encode;
# needs ffmpeg on PATH or SUNO_FFMPEG=/path/to/ffmpeg)
suno-dl --url https://suno.com/@yourhandle --extract-audio
```

### Courtesy pacing

Bulk syncs space their media transfers by the previous track's playback
duration, so an import of a few hundred songs is not a rapid-fire download of
a few hundred files:

```bash
# On by default for download/sync runs.
suno-dl --sync

# Opt out for a small, already-paced collection.
suno-dl --sync --no-playback-paced
```

The gap is measured between transfer *starts* and follows one bulk run across
every profile and playlist, so switching sources does not reset it. A missing,
zero, negative, non-finite or implausibly long duration uses a bounded four
minute fallback (clamped to one hour), never a multi-hour stall. Tracks that
are already archived, capped by `--max-tracks`/`--max-disk-mb`, video-only
skips and `--dry-run` never wait and never issue a media request. This is load
management only: it is not a substitute for permission to download, and the
per-host `--rate-limit` still applies independently.

### Dry run before committing

```bash
suno-dl --url https://suno.com/@h --dry-run
```

Prints the exact target path for every track and writes nothing.

### Preview without a browser

```bash
suno-dl --url https://suno.com/song/<uuid> --no-browser
```

Useful in headless/CI environments where Playwright is not installed. The
strict default keeps browser fallback enabled; if the feed fails and the optional
browser extra is absent, the CLI exits with installation guidance. Use
`--no-browser` only if a potentially incomplete profile result is acceptable.

## Tags & Music Assistant

Every saved track is written with embedded metadata:

| Field | Value |
| --- | --- |
| `artist` | the producer independently verified from the clip's own canonical page |
| `title` | the full track title, punctuation included |

Unverified producers are refused rather than replaced with the source profile
or playlist owner. Source provenance and creator identity remain separate.
Indexed older recordings are reused unchanged; repairs require the offline
migration with all writers stopped.

Without tags a player has to guess from the filename, and the usual guess is
wrong: Music Assistant shows `[unknown]` for the artist and, because it treats
the first `.` in a basename as the extension boundary, truncates a title at
`feat` — `drinko_Song (feat. Alice).m4a` reads as `drinko_Song (feat`.

Two things fix it, and both apply automatically on every download:

1. **The tags themselves** — the full title round-trips in `©nam`.
2. **Tag-guessing-proof filenames** — a mid-name period is written as `_`, so
   new files are `drinko_Song (feat_ Alice).m4a`. The period is only a display
   detail; it lives in the tag.

The `tagged=N` field in the summary reports how many files were tagged.

### Backends

Tags are written with **Mutagen** on a private staged copy. The FFmpeg tagging
fallback is disabled while complete metadata preservation is unresolved. The
current staged writer requires supported Linux filesystem checks and refuses to
replace a file when it cannot verify them. Without Mutagen or those checks, the
file is still downloaded but remains untagged, with a warning naming the file.
Install Mutagen with:

```bash
pip install -e ".[tags]"   # optional, mutagen-based tag writing
```

### Retagging an existing archive

An archive downloaded before tags existed has no metadata and cannot get it
from a re-download (the files are already there, and the tool would skip them
anyway). Embedded tag updates are currently unavailable while metadata
preservation is being verified. When available, this command selects files
without a title tag:

```bash
suno-dl --retag ~/suno-archive/downloads --retag-archive-root ~/suno-archive/downloads
```

- For a mutating retag, `--retag-archive-root` must identify the same authoritative
  root used by sync, even when `--retag` targets a subdirectory. All writers that
  ignore the archive lock must be stopped. Dry runs do not require this option.
- **No network access.** Nothing is fetched, renamed, or re-encoded.
- **Files with a title tag are skipped**, including when their artist differs
  from an explicitly supplied `--artist`. If a tag replacement reports uncertain
  publication, stop and inspect that file before any retry; the existing-file
  paths do not yet retain a durable uncertainty marker across runs.
- For selected files, the handle is taken from the filename (`<handle>_<title>`);
  `--artist` supplies one value for those files:

```bash
suno-dl --retag /media/suno --retag-archive-root /media/suno --artist drinko
```

- `--dry-run` shows what would be tagged without touching a byte.
- Exit code `1` if any file could not be tagged; the summary line reports
  `scanned` / `tagged` / `already_tagged` / `failed`.

Filenames are left exactly as they are, so Music Assistant keeps the paths
(and play counts) it already indexed.

### Repairing metadata without re-downloading (#159)

`--retag` can only guess the artist from the filename. When the public
metadata is known to be better — a mixed-creator playlist that credited the
playlist owner — reconcile it from freshly discovered public data:

```bash
suno-dl --url https://suno.com/@drinko --metadata-only --dry-run   # review
suno-dl --url https://suno.com/@drinko --metadata-only             # apply
suno-dl --url https://suno.com/@drinko --update-existing           # sync + repair
```

- Matching is by validated clip UUID across the whole archive, never by title
  or filename. Legacy profile folders are found too.
- `--metadata-only` never requests media: missing clips are reported as
  `metadata_missing`, not downloaded.
- `--update-existing` downloads missing tracks as usual and repairs the ones
  already owned.
- `--dry-run` prints `field: old -> new` for each clip and writes nothing.
- An unresolved producer never replaces existing attribution: unknown public
  identity keeps the archived tag and counts as `metadata_unresolved`.
- The audio stream, filename, cover and path are untouched; a failed tag write
  leaves the sidecar unchanged and counts as `metadata_failed` (exit code 1).

### Producer migration and writer coordination

The offline producer migration is work in progress and requires independent
review before it is used. Its archive lock is advisory: it cannot stop a
process that does not acquire the same lock for the same authoritative archive
root. Applying requires the operator to independently review the producer
decisions and entire plan, assert that review, supply the matching producer
mapping and the digest of the reviewed plan. Applying refuses missing or
placeholder producer decisions; read-only planning may still show them for
investigation. Pending journals without an explicit validated producer
decision cannot be resumed by the current migration version. Resuming requires a fresh review
assertion and the digest of the exact journal bytes inspected, including for a
preexisting journal. A digest binds an assertion to specific content; it does
not establish producer provenance or prove that a human reviewed it. Do not
populate review arguments automatically from untrusted evidence or journal data.

Before planning the cutover, stop every sync, retag, metadata repair,
library-management script, old Trackie version and other writer with access to
the archive. Keep them stopped through migration, any journal recovery, and
the subsequent review. Confirm that none will restart automatically.

Current CLI sync and mutating CLI retag paths acquire the shared archive lock
and check for an active migration journal. The protected CLI retag path requires
the explicit authoritative root. Direct Python helpers such as `tag_file`,
`retag_tree`, metadata reconciliation and `write_sidecar` do not independently
acquire that lock or check the journal. Calling them directly, using an older
release, using a different archive-root path, or writing through another
application can bypass the guard. A `writers_stopped=True` argument is an
operator assertion; it does not discover or stop these writers.

Do not delete migration journals, private staging files, or retained
`.library/producer-completed-*.json` records to make a blocked operation
proceed. Completion records are retained to prevent a completed journal that
reappears after a crash from being replayed against later library changes.
The reviewed plan includes streaming hashes and filesystem metadata for each
claimed media file, cover and sidecar. Creating the plan and rechecking it
before application each read those files in full and may take a long time.
These snapshots can miss concurrent file or metadata changes, including
equal-size rewrites and xattr races. They do not replace the requirement to
keep every other writer stopped throughout planning, review and cutover.
A plausible producer string and placeholder filtering are not proof of its
identity: independently verify every clip's attribution.

The journal and completion records require trusted private `.library` state.
Hashes are not authentication: another process with write access under the
same user can forge them. Stop if private state has been manually changed or
unknown writers may have accessed it; this trust assumption is separate from
the checks against unowned music files. Malformed or mismatched records and
unsupported older journals require investigation. The protocol assumes
cooperating writers and a supported local
filesystem that honors file and directory sync; it does not establish safety
on all network filesystems.

### One recording, several playlists (#158)

The archive is indexed by clip UUID across every subfolder. A track already
saved under `drinko/` is reused when the same UUID appears in a playlist, so
it costs one transfer and one file. The ordered membership is recorded
separately in `.library/membership.json`; audio is never copied or moved for
it, and a change in membership never deletes music.

### Validating and replacing a recording (#167)

```bash
suno-dl --url https://suno.com/@drinko --force-redownload --dry-run  # review first
suno-dl --url https://suno.com/@drinko --force-redownload
```

`--force-redownload` is the only path that replaces existing audio. Each
current file is probed read-only first (container sniff plus minimum size) and
reported as `invalid_existing` when it does not look playable. The replacement
is streamed to `.part` and swapped in atomically only after it validates, so a
failed transfer, HTTP error or bad payload leaves the previous recording
intact. A stale container of the same stem (for example a `.mp3` superseded by
an `.m4a`) is removed only after the new file is in place. It is incompatible
with `--metadata-only` and never runs implicitly.

### Files renamed by this change

Only titles that actually contain a period are affected. New downloads of
those tracks get the `_` form; a sync recognises an existing pre-1.6
period-bearing file and skips it, so nothing is downloaded twice. Retag
before the next sync if you want *every* file to carry tags, or just let the
next sync tag the rest as they arrive.

### Identify yourself

```bash
SUNO_USER_AGENT="my-archiver/1.0 (contact@example.com)" suno-dl --url ...
```

The default UA already identifies the tool honestly; override it if you are
running a fork or want to include contact details.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean run |
| 1 | Partial failure (at least one track failed) |
| 2 | No tracks found / usage error |

## Scripting

Because exit codes are meaningful and output is line-oriented, you can drive it
from a scheduler and alert on non-zero:

```bash
if suno-dl --url https://suno.com/@h --output-dir /srv/music; then
  echo "archive ok"
else
  echo "archive problem (exit $?)" | mail -s "suno-dl" you@example.com
fi
```

## Web dashboard

Prefer a browser to a terminal? The same CLI ships a localhost dashboard:

```bash
cd ~/suno-archive && suno-webui          # then open http://127.0.0.1:8760/
```

It shows archive stats per profile, the watch list, the blocklist, and the
configured decision engine — plus buttons that run the *real* CLI for you:

- **Sync (dry-run)** / **Sync (download)** — identical to `suno-dl --sync`,
  including the `.blocked` file and human-paced defaults
- **Rank** — `suno-recommend` with your candidates, taste signal optional

Output streams live into the page; runs execute one at a time (the same
politeness rules as the terminal). By default the server binds to 127.0.0.1,
rejects non-localhost Host headers, and never displays or stores secrets —
your session cookie stays out of the browser entirely. Port busy? Pass
`--port`. Use `--archive` to select another archive directory.

**LAN mode is opt-in.** With no gateway variables set it has no user-login
layer, so keep port 8760 restricted to trusted devices or place the dashboard
behind an authenticated reverse proxy. Two operator policies are available
(#162):

| Variable | Meaning |
| --- | --- |
| `TRACKIE_GATEWAY_REQUIRE_AUTH=1` | HTTP Basic on **every** request, reads included. Takes precedence over every LAN bypass. Startup fails closed if the credentials are not set. |
| `TRACKIE_GATEWAY_TRUSTED_CIDRS` | Comma-separated CIDRs whose **direct TCP peer** may read without credentials when `TRACKIE_GATEWAY_LAN_READONLY=1`. |
| `TRACKIE_GATEWAY_LAN_READONLY=1` | Read-only for peers inside the CIDRs; every write always requires credentials. |
| `TRACKIE_GATEWAY_USER` / `TRACKIE_GATEWAY_PASSWORD` | Credentials compared in constant time; never logged or echoed. |

Trust is the socket peer only: `X-Forwarded-For`, `Forwarded`, `X-Real-IP`
and `Host` are never used to decide access, so NAT, Docker publish, Tailscale
or a reverse proxy cannot mint a trusted client. Once any policy is set, a
state-changing request without valid credentials is refused (401 with a
`WWW-Authenticate` challenge when credentials exist, 403 otherwise) — there is
no loopback exception. Browser-origin/Host checks still apply to every write;
Supervisor ingress keeps its own authentication path.

### Home Assistant & Music Assistant

The dashboard doubles as a data source for a Home Assistant integration:
[`ha-suno-archive`](https://github.com/matthewhand/ha-suno-archive) polls
this API, shows archive sensors and sync buttons in HA, and mirrors every
audio file into HA's media directory — where Music Assistant's *filesystem*
provider indexes it for playback on any player.

```
suno-dl ──► ~/suno-archive/downloads/<profile>/*.m4a
        → suno-webui  (/api/status, /api/files, /media/…)
        → ha-suno-archive → <HA media>/suno/<profile>/*.m4a
        → Music Assistant filesystem provider
```

The dashboard must be reachable from HA (LAN mode: `suno-webui --host
0.0.0.0`). Pulls are audio-only and incremental (missing or truncated
files only, by size), so steady-state polls cost one tiny JSON request.
