# Changelog

## 1.6.2rc1 — 2026-11-06

Linux preview of Trackie Assistant: verified producer attribution, additions-only
playlist downloads, audio/artwork fallback, queue/storage UI and maintained
container images. See [release notes](docs/releases/v1.6.2rc1.md) for installation,
validation and explicit preview limits.


## Local Linux release audit candidate

- Align platform and media documentation with the guarded Linux additions-only
  engine; retire an obsolete broken demo.
- Keep scanner-test credentials synthetic and harden the local health probe
  against redirects, proxies and invalid configured ports.
- Prepare a fresh-history public snapshot separately from private development.


## Local candidate: sync queue and destination capacity

- Show queued tracks, an indicative saved audio/artwork size range and destination
  free space. Unknown lengths use an explicit four-minute assumption.
- Distinguish final storage from unknown temporary-video traffic and working
  space. Existing free-space safety checks remain authoritative.
- Keep current-batch standalone telemetry separate from selected-playlist totals;
  older workers without the new fields show unavailable instead of invented values.

## Local candidate: Trackie Assistant package identity

- Rename the Python distribution to `trackie-assistant`; keep existing CLI commands,
  Python imports, environment variables and HAOS add-on IDs compatible.
- Update bundled-wheel builds and optional-install guidance. Standalone upgrades
  must replace the old distribution rather than install both into one environment.


## 1.6.2 development candidate (Linux, local only)

- Verify producer, creator handle and title against the exact canonical song
  page before naming or tagging new recordings.
- Prepare decodable M4A audio, tags, sidecar and requested cover/lyrics together;
  recover interrupted publication from durable UUID/inode/hash intents.
- Reuse indexed recordings unchanged; preserve legacy pruned UUIDs and refuse
  pending or ambiguous filesystem state.
- Use strict public playlist membership and preserve public rows in mixed
  playlists. Pin individual-song selection to the requested UUID.
- Keep indexed repairs behind the reviewed offline producer migration.
  Automatic repair/replacement/rotation, video retention, sidecar-free and
  non-M4A modes remain explicitly unavailable in this candidate.
- Normalize export and build permissions for reproducible wheel provenance.


All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Release candidate alignment (not yet tagged or deployed)
- **Trackie Assistant** is the visible product name; the original approved
  Trackie Dacks hero is bundled in the Python wheel and the WebUI shows a
  readable CSS crop. The package distribution, CLI commands, add-on slug
  `suno_dl`, and `local_suno_dl` identity remain unchanged.
- Python distribution and CLI **1.5.3** replace the previous **1.5.2**
  wheel with a distinguishable release candidate. The Home Assistant add-on
  manifest becomes **1.6.1**, with display name and panel title both
  **Trackie Assistant**; the add-on vendors exactly one matching **1.5.3**
  wheel. None of these versions is asserted to be published or deployed.
- Source, embedded wheel, add-on manifest, and documentation are checked
  together by synthetic release-artifact regressions. The latest published
  GitHub release remains `v1.5.0` until separately authorized publication.


### Added
- **Public playlist expansion with resumable checkpoints** (#160). `--playlists`
  (`SUNO_PLAYLISTS`) also archives the ordered membership of every explicitly
  public playlist a profile advertises, which is where recordings the profile
  no longer shows tend to live. Private, unlisted, hidden, trashed and deleted
  listings are never selected; membership is read from the playlist's public
  page and stored as ordered UUID references in `.library/membership.json`, so
  the #158 library still keeps one recording per UUID. A page that exposes no
  song ids is reported **unresolved** instead of being counted as empty, and is
  retried next run. Progress is checkpointed atomically in
  `.library/playlist-checkpoint.json`: a restart re-reads only the playlist in
  flight, an unchanged import does nothing, and a renamed or resized playlist
  is re-expanded. `--dry-run` writes neither the checkpoint nor membership.
  Pages, playlist count, tracks per playlist and response size are all capped,
  and playlists are read one at a time under the existing pacing.
- **Version-controlled CT107 deployment** (#173). `deploy/ct107/` now carries
  the Dockerfile, `compose.yaml`, entrypoints and runbook the running stack was
  built from, so a clean checkout reproduces the image instead of relying on
  files that only existed on the host. The build refuses a base interpreter
  that is not 3.13, zero or multiple wheels, and a wheel whose version differs
  from `CLI_VERSION` — the 3.12-image-with-a-3.13-wheel mismatch that blocked
  the last rebuild now fails at build time with a clear message. Both the
  gateway and the worker declare Docker healthchecks; the worker stays behind
  `TRACKIE_REAL_SYNC_APPROVED` (0 = intentional standby, #136) and never
  passes `--rotate`. Rollback is documented as an image retag plus
  `up --no-build`, and the runbook states that the committed files must be
  reconciled with the host before the next deployment.
- **Bounds on untrusted content** (#157). Every response body is now read in
  chunks and refused above 12 MiB (HTML/RSC pages, JSON feeds) instead of
  being buffered first, the feed JSON must be an object, cover art aborts at
  an absolute 8 MiB per image, sidecars above 256 KiB are ignored before they
  are parsed, lyrics sidecars above 64 KiB are refused, and track title/
  lyrics/tag text is truncated to generous fixed bounds (1 k / 100 k / 4 k
  characters) with nonsense counters dropped. Sizes are checked before the
  bytes are allocated, and existing files are never damaged when a bound
  trips.
- **Operator-defined gateway access policy** (#162). `TRACKIE_GATEWAY_REQUIRE_AUTH=1`
  forces HTTP Basic on every request (reads included) and refuses to start when
  the credentials are missing; `TRACKIE_GATEWAY_LAN_READONLY=1` with
  `TRACKIE_GATEWAY_TRUSTED_CIDRS` allows unauthenticated reads only from those
  direct TCP peers. Every write needs credentials once any policy is set (no
  loopback exception), forwarded IP headers are never trusted, and credentials
  are compared in constant time and never logged. Defaults remain unchanged.
- **Global clip-UUID library** (#158). The archive is indexed by validated Suno
  UUID across every subfolder, so one recording referenced by two playlists is
  transferred once and stored once; legacy per-profile folders are reused
  in place, never renamed or deleted. Playlist membership is written
  separately to `.library/membership.json` as ordered UUID references. Two
  sidecars claiming one UUID for different physical files fail closed and are
  reported rather than silently resolved.
- **Metadata reconciliation** (#159). `--metadata-only` repairs the artist,
  title and sidecar of already-archived clips and never issues a media
  request; `--update-existing` performs the same repair during an ordinary
  sync; both honour `--dry-run`, which prints the proposed field changes and
  writes nothing. Matching is by UUID only, an unresolved producer never
  overwrites existing attribution, filenames and audio streams are untouched,
  and a failed tag write leaves the sidecar unchanged. The sidecar now records
  a separate `artist` field alongside the source `handle`.
- **Verified replacement** (#167). `--force-redownload` is the only way to
  replace existing audio: it probes the current recording read-only (container
  sniff and minimum size), reports invalid files, streams the replacement to
  `.part` and swaps it in atomically only after it validates, removing a stale
  container of the same stem. A failed transfer leaves the previous, playable
  recording untouched. It cannot be combined with `--metadata-only`.
- Bulk `download`/`--sync` runs are playback-paced by default (`--playback-paced`):
  each real media transfer starts no sooner than the previous track's duration
  after the previous one began, keeping one spacing across every source in the
  run. Unknown, zero, negative or non-finite durations use a bounded four
  minute fallback (hard cap one hour) instead of raising or stalling; already
  archived, capped, video-only and `--dry-run` tracks never wait and never
  fetch media. `--no-playback-paced` restores the previous timing (#161).
- `scripts/release_manifest.py` (`make manifest`) pins a wheel to one git
  commit: it exports the ref with `git archive`, builds with
  `SOURCE_DATE_EPOCH` set to the commit time so two builds of the same
  commit produce the same SHA-256, refuses to proceed when a shipped
  `suno_dl/` member differs from the committed file, and records the commit,
  wheel hash, per-member hashes, `Requires-Python`, requirements and build
  toolchain as JSON (#163).
- A tracked-file hygiene test fails if source, tests, docs or workflows start
  naming private-range addresses (`10/8`, `172.16/12`, `192.168/16`,
  `100.64/10`) or site-specific hosts (`ubuntu-<name>`, `*.local`,
  `*.ts.net`). The intentional SSRF fixture in `tests/test_urls.py` is the only
  allow-listed address (#107).
- Genre card + web page: long actions (Split, Ask AI, Save, Undo, Reclassify, Sync, Recommend) show a spinner,
  one live status line (`analysing 18 of 42 tracks`), a progress bar and Cancel, and disable every other action while
  they run. Failures become a banner naming the cause class (AI timeout / unreachable / invalid or empty reply /
  file or permission / unexpected), what completed, a request id + time, Retry and Copy details (#95).
- Web UI API errors use one envelope `{code, message, detail, request_id}` with the right HTTP status; the full
  cause is logged (secrets redacted). Runs expose `progress` and can be cancelled (`POST /api/runs/<id>/cancel`).
- HA conversation agents that answer with `response_type: error` are reported as the AI service failing instead of
  "the agent did not return JSON"; an empty/unusable assignment reply no longer silently lumps songs into the last
  subcategory (#95). Songs the AI omits from a partial assignment reply are no longer guessed into the
  last subcategory: the split preview says how many were not classified and they stay in the parent (#95).
- **Rolling library caps** (#93). Three new options, all default off so current
  behaviour is unchanged: `--max-tracks N` (`SUNO_MAX_TRACKS`) keeps only the
  newest N tracks, `--max-disk-mb N` (`SUNO_MAX_DISK_MB`) caps the archive size,
  and `--rotate` (`SUNO_ROTATE`) deletes the oldest tracks after a sync until
  the archive fits both caps. With `rotate` off a cap stops *downloading* at the
  limit and nothing is ever deleted. Deletion removes a track's sidecars too
  (`m4a`/`mp4` media variants, cover images), orders by Suno `created_at` with
  file mtime as fallback, keeps the state in `.suno-rotation.json` so pruned
  tracks are not re-downloaded, stays inside the archive dir (never through a
  symlink), logs every removal, honours `--dry-run`, and refuses a pass that
  would delete the entire archive. HA add-on: options `max_tracks`,
  `max_disk_mb` (both 0 = off) and `rotate` (default false).

### Fixed
- Rotation is now crash-consistent (#147). A durable journal records the
  planned groups before the first unlink, every successful removal, and
  completion. A run interrupted mid-prune reconciles on the next start:
  a group whose media is entirely gone is marked pruned and forgotten, a
  group with media left keeps its state entry so it is never suppressed,
  and recovery never deletes anything (already-deleted files cannot be
  restored). `--dry-run` writes no journal and no state.
- Each song in a mixed-creator playlist is credited to its own producer. Playlist
  and profile rows that carry the producer as a nested object (`user`,
  `author`, `creator`, `profile`) instead of flattened `display_name`/`handle`
  fields were previously unresolved or fell back to the profile owner; the
  nested identity now goes through the same Unicode/control/bidi/oversize
  validation and the canonical handle is kept separately (#157).
- Redirect chains longer than one hop are followed (bounded at five) instead
  of surfacing as `FetchError`. Signed CDN URLs routinely bounce twice, and
  only the first hop used to be followed before `raise_for_status()` turned
  the second redirect into a download failure. Every hop is still checked
  against the https media allow-list, so a later hop to an internal address
  is refused exactly as before.
- The web UI's Host guard rejected a legitimate IPv6 loopback request: it
  split the header on `:` before removing the brackets, so `Host: [::1]`
  (sent by a browser on the default port) became `":"` and returned 403, and
  the expanded `[0:0:0:0:0:0:0:1]:8760` was rejected the same way. Brackets
  now come off first and IP literals are compared in compressed form.
- HTTP retries honour `Retry-After` on 429/5xx responses (both the
  delay-seconds and HTTP-date forms, clamped to 60 s) instead of always
  waiting the 0.5 s exponential backoff, so a rate-limited run backs off for
  as long as the server asked rather than immediately tripping the limit
  again.
- Genre card chips no longer hide behind an invisible sideways scroll on a
  narrow card (#133). They wrap, the top row clamps to three rows and a
  `More ▾` / `Less ▴` toggle reveals the rest, so every top-level genre is
  reachable at 320–390 px. The sticky header stays under ~40% of the viewport
  while collapsed and the page never overflows horizontally. The card
  cache-buster is now `?v=9`; a deployed HA copy needs its own resource bump.
- Genre card / web page layout and accessibility, checked in a real browser at 360x800, 390x844, 768x1024, 1280x720 and
  1920x1080 (#95): the working banner and Cancel were hidden under the sticky genre chips on a phone; chips squashed
  over each other once tap targets grew; song rows collapsed beside the detail pane at tablet width; controls are now
  >= 44px on touch and narrow cards (>= 24px with a mouse); muted text meets WCAG AA contrast and the card paints its
  own dark surface so a light HA theme cannot make it unreadable; keyboard focus is no longer lost on every re-render,
  moves into the detail pane when a genre is opened with the keyboard and returns to the chip on Esc; progress and
  results are announced through persistent `aria-live` regions (a failure is spoken once, not on every re-render);
  the finished split preview opens the sheet and scrolls into view on a phone; titles are shown exactly as stored
  (whitespace preserved, long ones truncated visually only, full text in the tooltip).
- Home Assistant add-on ingress: the dashboard/API is now reachable on the
  add-on's internal network instead of only loopback, so Home Assistant's
  ingress proxy can serve the panel. The listener accepts requests only from
  the Supervisor's resolved peer address and ignores spoofed
  `X-Forwarded-For`/`X-Ingress-Path` headers; no host port is published (#137).
- `mypy src` (a CI gate) failed because the Windows branch of the archive lock
  used `os.name`, which mypy cannot narrow; typeshed only declares
  `msvcrt.locking` for win32. It now branches on `sys.platform`, so Linux and
  `--platform win32` analysis both pass, with a synthetic Windows locking test.
- The release-artifact contract sampled only two packaged files, so the
  vendored wheel silently drifted; it now compares every packaged module.
- Song-page parser reads Suno's new `song_schema` state and falls back to the
  canonical `/song/<uuid>` id instead of the og:image asset id (#96).

### Changed
- Python **3.13** is the only supported interpreter (#121). `requires-python`,
  the classifiers, the Ruff and mypy targets, the GitHub CI/release matrices,
  `scripts/ci-local.sh` and the add-on image all move from 3.10–3.13 to 3.13,
  and docs no longer advertise 3.10–3.12. Those versions are untested and
  unsupported; the vendored wheel and its `Requires-Python` follow.
- Playwright is an **optional** `browser` extra instead of a base runtime
  requirement (#125). A plain install keeps the HTTP feed for songs, playlists
  and profiles; the profile browser fallback needs
  `pip install 'suno-download-cli[browser]'` and
  `python -m playwright install chromium`. `--browser` stays on by default, and
  a missing browser or Chromium binary now fails with that guidance instead of
  silently archiving fewer tracks; `--no-browser` opts into HTTP-only discovery.
  The Home Assistant add-on still installs Playwright and Chromium explicitly.
- Downloads default to **audio + cover art**; **videos are opt-in** (#85).
  CLI: `--videos/--no-videos` (default off), `--covers/--no-covers` (default on),
  env `SUNO_DOWNLOAD_VIDEOS` / `SUNO_COVERS`. `--audio-only` remains an alias for
  `--no-videos`. Webui sync checkbox for videos (off) and covers (on). HA add-on
  options: `download_videos` (default false), `covers` (default true); legacy
  `audio_only` still accepted as the inverse of videos.

### Added
- Per-track **id sidecar** (#97): next to each saved track suno-dl writes
  `<same name>.json` with the permanent Suno clip id, `url`, the original
  (unsanitised) `title`, `handle`, `created_at`, `model_name`/`model_version`,
  `duration_sec`, style `tags`, `is_public`, `saved_files`, `suno_dl_version` and
  `downloaded_at` (UTC). Public metadata only, no secrets. A track whose id is
  already recorded by a sidecar is skipped even if its filename differs, and a
  track already on disk gets its sidecar on the next sync. `--sidecar/--no-sidecar`
  (env `SUNO_SIDECAR`, default on); `sidecars=N` appears in the run and sync
  summaries.
  Rotation (#93) deletes a pruned track's id sidecar along with the track, but
  only a genuine suno-dl sidecar that lists that track's own files; any other
  `.json`, a sidecar of an unpruned track, and symlinks are never touched.
- `scripts/backfill_sidecars.py`: writes sidecars for an existing archive from a
  library/clips JSON. Local only; matches with `build_filename` (both `feat.` and
  `feat_`, `_N` suffix), never overwrites a sidecar, never touches media, supports
  `--dry-run`, and reports ambiguous (same-title) clips and unmatched files.
- Every saved track now carries embedded `artist` / `title` tags, so Music
  Assistant credits the profile (`drinko`) instead of showing `[unknown]`
  (#69). `artist` is the handle the track was synced from, `title` is the full
  track title. Tags are written with **mutagen** (new optional `[tags]` extra)
  and fall back to `ffmpeg -c copy` when mutagen is absent — metadata only,
  never a re-encode. With neither backend available the file is still saved,
  just untagged. `tagged=N` now appears in the run and sync summaries.
- `suno-dl --retag <dir>`: one-shot migration that writes the same tags into
  an archive downloaded before tags existed. Local only — no requests, no
  renames, no re-download; already-tagged files are skipped, so it is safe to
  re-run. `--artist` overrides the handle inferred from each filename, and
  `--dry-run` previews it. Exits 1 if any file could not be tagged.
- `suno-webui --ingress`: disables the Host-header guard for serving behind
  a trusted reverse proxy (Home Assistant ingress), where Host is a
  proxy-internal name. The dashboard page now labels the watch list
  "Sync profiles".

### Fixed
- Feed discovery works again: it called `/api/profiles/<handle>/songs` (HTTP 404, silently treated as an empty
  profile) and now uses `/api/profiles/<handle>?clips_sort_by=created_at&playlists_sort_by=created_at&page=N`, stops
  at `num_total_clips`, and logs a 404 as a warning. `--no-browser` and `network-styles` find every public clip (#113).
- Titles containing `feat.` are no longer truncated in Music Assistant (#70).
  Two independent fixes, because the truncation had two causes:
  1. Embedded tags (above) — the full title now round-trips in `©nam`.
  2. Media filenames no longer contain a mid-name period, so a tag-guessing
     player cannot mistake `Song (feat. Alice).m4a` for the title
     `drinko_Song (feat` plus extension ` Alice).m4a`:
     `drinko_Song (feat_ Alice).m4a`.

  Archives created before this change keep working: the downloader recognises
  the old period-bearing name for the same track and skips it rather than
  fetching a second copy, so the migration is `suno-dl --retag`, not a
  re-download. Only titles that actually contained a period are renamed on a
  fresh download; everything else keeps its filename.

### Changed
- `[tags]` optional extra added (mutagen). The default install is unchanged;
  without it, tagging uses ffmpeg when present and is skipped otherwise.
- CLI version is now `1.5.1` — the tagging and `--retag` work above was never
  part of a tagged release (the last tag is `v1.5.0`), and the bump is what
  makes a deployed add-on verifiable via `suno-dl --version`.
- Home Assistant add-on `1.5.2`: the vendored wheel is now `1.5.1` (the
  `1.5.0` wheel is removed — the Dockerfile's `COPY` glob would match both),
  and the image installs mutagen so tags are a single in-place edit rather
  than an ffmpeg remux. The add-on version and the CLI version are separate:
  `1.5.2` ships CLI `1.5.1`. The upgrade runbook in `addon/suno_dl/DOCS.md`
  now uses `ha apps rebuild local_suno_dl` (not `update`) and a disk-space
  preflight, since the rebuild needs several GB of headroom.

## [1.5.0] - 2026-09-28

### Added
- Media-pull API for remote integrations (e.g. a Home Assistant custom
  component syncing the archive into Music Assistant): `GET /api/files`
  returns a flat per-profile audio listing, and `GET
  /media/<profile>/<file>` streams a single file. Audio-only (`.m4a`,
  `.mp3`, `.wav` — the mp4 video twins are neither listed nor served),
  with naming-style containment checks: traversal, hidden files,
  unexpected depth, and non-audio suffixes all 404. The listing includes
  per-file sizes so remote pullers can detect and re-fetch truncated
  transfers.

## [1.4.0] - 2026-09-28

### Added
- Local web dashboard: `suno-webui` (default port 8760) serves a status page —
  archive sizes per profile, watch list, blocklist count, decision engine —
  with buttons to run the real CLI (sync dry-run/download, recommend with
  taste) as subprocesses whose output streams into the page. One run at a
  time (the politeness charter holds in the browser); binds to 127.0.0.1 by
  default and rejects non-localhost Host headers (DNS-rebinding guard); no
  secrets ever cross the UI boundary. Stdlib `http.server` only — no new
  dependencies.
- `--host` option for `suno-webui`: explicitly expose the dashboard to the
  LAN (warning banner, no auth — treat the network as trusted). The
  Host-header guard still accepts only this machine's own names, so
  attacker-chosen DNS names resolving at the server are refused even in
  LAN mode.

## [1.3.0] - 2026-09-28

### Added
- Track blocklist: a `.blocked` file in the working directory (next to
  `.env`; `SUNO_BLOCKED_FILE` overrides) lists clip IDs the tool must never
  fetch — one UUID per line, `#` comments. Blocked tracks are skipped before
  any request in every mode (dry-run included) and reported as a new
  `blocked=` counter in the summary; malformed lines raise `ConfigError`.
  Motivated by Suno 403-ing one specific track for anonymous fetches
  (verified 2026-09-28): repeatedly knocking on a blocked object risks the
  rate-limiting the human-paced defaults exist to avoid, so backing off is
  now explicit and durable.
- Taste-aware ranking for `suno-recommend`: `--taste-from <archive-dir>` (env
  `SUNO_TASTE_FROM`) mines track titles from your existing `suno-dl --sync`
  archive (any depth; m4a/mp4 pairs dedupe; capped at 40) and feeds them into
  the JEV decision state, so candidates are ranked for similarity to what you
  actually keep. The engine label gains a `+taste` suffix so the output is
  honest about what influenced it; if no JEV engine is reachable, ranking
  falls back to the deterministic heuristic (which never pretends to know
  your taste).
- Opt-in follow-list expansion for `suno-recommend`: `--session-cookie` (env
  `SUNO_SESSION_COOKIE`) with `--expand-following`/`--expand-followers` pulls
  the seed's follow lists from the user's own authenticated session and ranks
  the whole graph neighborhood (capped at 20 candidates). The cookie travels
  in request headers only and is never echoed or logged. Without it, ranking
  behavior is unchanged (public-signals only; follow-list endpoints 404
  without a session — verified 2026-09-28).
- SystemOne decision-client abstraction (`suno_dl.decisions.DecisionClient`):
  one client for every `{state, questions}` decision provider — the local
  JEV fleet, self-hosted Laya, and hosted SystemOne services (TypeSafe Jev,
  Venice.ai, Cloudflare Workers AI, Zait Labs) via `--jev-key`/`SUNO_JEV_KEY`
  Bearer auth. Normalizes all observed answer spellings (`label`, Laya's
  `choice` + `probabilities`, bare `probabilities`, binary `noul`); refuses
  single-option questions (several providers 422 them) so the heuristic
  fallback engages honestly. The recommender consumes this instead of its
  own request/response handling.
- Laya compatibility for `suno-recommend`: the on-host Laya decision service
  (`POST /v1/decisions`) names its winner `choice` with per-option
  `probabilities` rather than the JEV `label` field; both shapes now parse,
  so `--jev-url http://127.0.0.1:8091/v1/decisions` works. A single-candidate
  question (which Laya rejects with 422) falls back to the deterministic
  heuristic as usual.

### Fixed
- `suno-recommend` env-backed options (`SUNO_JEV_URL`, `SUNO_JEV_MODEL`,
  `SUNO_JEV_KEY`, `SUNO_SESSION_COOKIE`, `SUNO_TASTE_FROM`) now honor `.env`:
  click resolves `envvar=` hooks before callbacks run, so `.env` loaded
  inside the callback was invisible. Options resolve after explicit `.env`
  loading (flag > environment/.env > built-in default); shared via
  `config.ensure_dotenv_loaded()`.

## [1.2.0] - 2026-09-28

### Added
- Per-source subfolders: downloads now nest under `<output-dir>/<handle-or-id>/` (derived from the source URL, sanitized against traversal); `--flat` restores the old single-directory layout.
- `--extract-audio`: after download, remux video-MP4 tracks to audio-only M4A losslessly (`ffmpeg -c:a copy`; no re-encode). Skips gracefully when ffmpeg is unavailable; honors `SUNO_FFMPEG` override.
- Watch-list sync: `--sync` walks every source in `SUNO_WATCH` (or repeatable `--watch <handle|url>`), syncing each into its own subfolder with existing-file dedup; a failed source is reported and skipped without aborting the run (exit 1).

## [1.1.1] - 2026-09-27

### Fixed

- **Critical:** media payloads are now structurally validated before being saved
  (MP4 `ftyp` box, MP3 ID3/frame sync, or RIFF/WAVE). Suno's primary cloudfront URL
  serves HTTP 200 with an *encrypted*, unplayable payload; the downloader used to
  save it as a corrupt `.m4a`. It now rejects invalid payloads and falls through to
  the next candidate (verified against the live CDN: all files now pass `ffprobe`).

## [1.1.0] - 2026-09-27

### Changed

- **User-facing:** downloads are human-paced by default — `--concurrency` now defaults
  to **1** (was 5) and a **jittered 1.5 s** per-host minimum gap between requests is
  enforced by default (pacing was previously off). Tune with `--rate-limit SEC` or
  `SUNO_RATE_LIMIT_SEC`; pass `0` to disable pacing, `--concurrency N` to go parallel.

### Added

- `--rate-limit` flag mirroring `SUNO_RATE_LIMIT_SEC`. Per-host intervals are randomly
  varied within [0.75x, 1.5x] so request spacing does not look metronomic.

### Fixed

- **Security:** signed-CDN query strings no longer reach logs or exception messages —
  fetcher errors and retry logs now use `urls.redact_url`.
- **Security:** redirects are now followed only when they stay on allow-listed https
  hosts; a signed CDN URL bouncing to an internal or non-https address is refused.
- Errors raised after retry exhaustion are now the typed `suno_dl.errors.FetchError`
  (still a `RuntimeError`), and the CLI reports `SunoError` as a clean message instead
  of a traceback.
- The default User-Agent now advertises `1.0` (was stale at `0.1`).

### Added
- `--audio-only` flag (opt-in): skip video-only tracks without a network
  request — the largest disk saving for archival runs. Tracks with both audio
  and video candidates still download the audio.
- `--covers` flag (opt-in): download each track's cover JPEG next to the audio
  (`<name>.cover.jpeg`). Best-effort: missing or blocked covers never fail a
  run. With the optional `[image]` extra (Pillow), covers are recompressed only
  when the result is strictly smaller.
- New `skipped_video` counter in the download summary.

## [1.0.0] - 2026-09-27

### Added
- Shared `suno_dl.urls` module as the single source of truth for UUID/host/CDN
  conventions, with a media-host allow-list and URL redaction.
- Shared `suno_dl.naming` module: Windows-safe filename sanitisation, path
  traversal defence, and collision handling.
- `suno_dl.errors` exception hierarchy.
- `--version` flag and meaningful exit codes (0 ok, 1 partial failure, 2 no tracks).
- CI (lint, format, typecheck, test matrix, wheel build + smoke test).
- LICENSE, CONTRIBUTING, SECURITY, CHANGELOG.
- Release pipeline quality gate: tags must pass the lint/type/test suite
  before artifacts are built or published.

### Changed
- Declared 1.0.0 (Development Status classifier: Alpha → Production/Stable).
  No user-facing CLI behaviour changed.

### Fixed
- `html` / media resolution now uses the song page's real `media_urls` instead of
  the stale `cdn1.suno.ai/<id>.mp3` convention (which returns HTTP 403).
- Signed CloudFront URLs are no longer logged; the `httpx` logger is silenced.
- Removed unbounded serial per-track fetches during profile discovery.
- Filenames containing dots no longer break idempotency checks.

### Security
- SSRF defence: media URLs are allow-listed by host; IP literals and
  credential-bearing URLs are rejected.
- Path traversal / unsafe filename defence with regression tests.
- Signed URL credential redaction in logs.

## [0.1.0] - 2026-09-26

### Added
- Initial release: profile/playlist/song discovery, CDN download, dry-run,
  concurrency, filename patterns.
