# Handoff note — suno-download-cli

_Last updated: 2026-09-28, master `82fc875`, v1.5.0 released._

This note is the durable record of the v1.0 release effort and everything
landed since: what changed, what was verified and how, and what was
deliberately left undone.

## Current priority and scope (2026-11-03)

The current target is Linux on CT107, Linux standalone CLI/web/API and HAOS,
with development on the Linux development host. Windows recovery is future, unscheduled work;
it must not block this delivery or consume further work without an explicit
user request. See [ROADMAP.md](ROADMAP.md).

Keep the active sync and live library safe. Older filename/artist corrections
require verified terminal sync success, own-clip producer verification,
independent plan review and an all-writers-stopped cutover. The recurring
monitor remains paused. The sections below are historical.

## Where the project stands

- **Releases:** `v1.0.0`, `v1.1.0`, `v1.1.1` — all tagged, each with sdist +
  wheel attached. Wheel installs verified in clean venvs at each cut
  (`suno-dl, version 1.1.1`). PyPI publishing remains intentionally stubbed
  out in `release.yml`.
- **Code:** 262 tests, 91.5% coverage; ruff, ruff format, and mypy all clean.
  Full gate: `bash scripts/ci-local.sh master`.
- **Repo hygiene:** single branch (`master`), 0 open issues, 0 open PRs,
  all stale branches removed after merge-verification.

## What changed, chronologically

### Foundation (v1.0.0)
- **#19–#28:** parser hardening, SSRF allow-list, signed-URL redaction,
  filename containment, local CI harness replacing GitHub Actions, release
  workflow with test gate, Playwright discovery covered via fake
  `sync_playwright`, version bump to 1.0.0.
- **#20–#23:** Dependabot action bumps (checkout@7, setup-python@7,
  download-artifact@8, action-gh-release@3) — all merged after changelog
  review; artifact names verified to match across upload/download.
- **#24–#26:** demo GIF and the 10-diagram visual documentation set.
- **#27–#28:** `_discover_sync` wiring tests; release build gated on
  lint/type/test.

### Post-1.0 additions
- **#30:** opt-in `--audio-only` and `--covers` disk-saver flags
  (default off; covers pass the SSRF allow-list; recompression only when
  smaller, via the optional `[image]` extra).
- **#31–#32:** release-verification runbook; diagram set updated for the
  new release test gate.
- **#33–#34:** `downloader.py` branch coverage closed 85% → 100% (statements
  and branches); inventory refreshed.

### Sweep findings (v1.1.0)
- **#35:** signed URLs no longer leak into exception messages (all pass
  through `urls.redact_url`); redirect SSRF bypass closed (redirects
  followed only onto allow-listed https hosts); `page_url` allow-list-checked
  before fetch; `.wav` no longer saved as `.mp3`; PNG covers never
  re-encoded as JPEG; typed `FetchError`; CLI reports `SunoError` without a
  traceback; UA updated from stale `0.1`.
- **#36:** human-paced defaults — `--concurrency` 5 → 1, jittered 1.5 s
  per-host gap on by default (`SystemRandom`, delay ×[0.75, 1.5]);
  `--rate-limit SEC` flag, `0` disables.

### Live-testing findings (v1.1.1) — the most important discovery
- **#40:** **every track previously downloaded was unplayable.** Suno's
  primary cloudfront URL serves AES-CTR-encrypted bytes with HTTP 200 and
  `audio/mp4` content-type. Size-only validation accepted them. The
  downloader now sniffs payload structure (MP4 `ftyp` / MP3 ID3 or frame
  sync / RIFF-WAVE), rejects invalid payloads with a redacted warning, and
  falls through to the next candidate — the unencrypted cdn1 MP4, verified
  playable via ffprobe against the live CDN.
- **#43:** the encryption mechanism is documented: per-object key + IV
  fetched from a license server via Suno's authenticated API; decryption is
  in-browser via WebCrypto. Suno's own bundle names it a DRM pipeline.
  Implementing decryption would mean driving a private authenticated API to
  bypass a technical protection measure — **out of scope by charter**
  (no-auth, no-private-API) and DMCA anti-circumvention territory.
- **#41–#42:** v1.1.1 release; runbook lists all three release runs.

### Recommender era (v1.3.0, 2026-09-28)
- **#48–#51:** watch-list sync, per-source subfolders, `--extract-audio`,
  .env-from-cwd fix. The drinko archive reached 171/172 tracks (2.4 GB,
  m4a+mp4 pairs) — the last track, "Spring Has No Alibi" (`7c68eb8d-…`),
  is 403-blocked by Suno for anonymous fetches on every format while its
  171 siblings download fine (per-object block, verified twice).
- **#53–#54:** `suno-recommend` — user-seeded profile ranking via LAN JEV
  engines (follow LISTS are 404 without auth; only counts are public),
  then opt-in follow-list expansion via the user's own session cookie.
- **#55:** taste-aware ranking — `--taste-from` mines titles from the
  archive (recursive walk, m4a/mp4 dedupe, capped at 40) into the decision
  state; engine label gains `+taste` only when the signal is real.
- **#56–#57:** the decision-provider abstraction — web research confirmed
  the SystemOne `{state, questions}` contract is converging across TypeSafe
  Jev, Venice.ai, Cloudflare, and Zait Labs (Laya-backed). All handling now
  lives in `suno_dl.decisions.DecisionClient` (answer-shape normalization:
  `label` / Laya `choice` / bare probabilities / `noul`; Bearer auth;
  refuses single-option questions). Also collapsed: shared `ensure_dotenv_loaded`,
  `fetcher.USER_AGENT`, a `mock_http` conftest fixture. Fixed en route:
  click resolves `envvar=` before callbacks, so `.env` never reached the
  recommender options. While the previously configured LAN JEV fleet was down, local Laya
  (127.0.0.1:8091) served as the engine; the archive `.env` defaults to it.
- **#58:** `.blocked` track blocklist — skip-before-any-request for clip
  IDs the user chooses never to fetch (straggler cron was explicitly
  rejected to avoid automation pressure). Discovered while wiring: `uv run`
  auto-exports the repo `.env` and can shadow the archive `.env`; the
  blocklist anchors on the working directory accordingly.
- **#59:** v1.3.0 released (tag on `af3a358`); release.yml budget-failed in
  7s as expected, artifacts built and verified locally, runbook updated
  (five release runs now await one rerun each after budget reset).

### Web dashboard era (v1.4.0, 2026-09-28)

- **#61:** `suno-webui` — localhost web dashboard driving the real CLI
  (sync dry-run/download, recommend with taste) as subprocesses with live
  output streaming; stdlib-only, one run at a time, no secrets in the UI.
  Live smoke: real dry-run through the HTTP API reached
  `Sync summary: total=172 blocked=1 failed=0`.
- **#62:** optional LAN mode (`--host 0.0.0.0`) with a parameterized
  DNS-rebinding guard (machine's own names accepted, foreign refused).
  Host quirk: `/etc/hosts` aliases the hostname to loopback, so machine
  address discovery needs `gethostbyname_ex` + a default-route probe.
- **#63:** v1.4.0 released (tag on `2381083`); release.yml budget-blocked
  as expected (all six test jobs 0 steps), artifacts built and verified
  locally (twine PASSED ×2, clean-venv smoke), GitHub release created with
  both artifacts; runbook updated (six release runs await rerun).

## How it was verified

- Every PR gated with `bash scripts/ci-local.sh <branch>` before merge
  (Python 3.13, ruff, ruff format, mypy, pytest, build + smoke).
- Live end-to-end testing against real Suno pages/CDN: song and profile
  dry-runs (exit 0, zero writes), timed download (3/3, observed
  1.24–1.56 s jittered spacing), resume re-run (`skipped_exists`),
  and ffprobe validation proving the media-validity fix.
- Release wheels smoke-tested in clean `uv venv`s (the host lacks
  `python3-venv`/ensurepip — use `uv venv`, not `python3 -m venv`).
- The `ci-local.sh` harness uses a fixed work dir per ref (now isolated by
  ref since #38) — do not run two gates for the same ref concurrently.
- `scripts/canary_sync.sh [archive-dir]` is the **live regression canary**
  (kept out of ci-local deliberately — the gate stays hermetic): it dry-runs
  the real sync against the real archive, so any Suno page-structure change
  or blocklist-wiring regression fails loudly with exit 1. Cheap enough to
  run after any parser/discovery change, or on a schedule.

## Deliberately left undone

- **GitHub Actions verification of releases.** The Actions budget on this
  account is exhausted; all release runs fail with jobs that never start
  (0 steps). This is budget noise, not code failure. When the budget
  resets: follow `docs/release-verification.md` — one `gh run rerun` per
  release run listed there. Releases were cut and verified locally in the
  meantime using the same steps release.yml runs.
- **PyPI publishing** — stubbed in release.yml by design; no secrets
  configured.
- **DRM decryption of the encrypted audio primary** — excluded on
  principle (see #43); the unencrypted cdn1 MP4 is the sanctioned path.
- **Peer-discovery / related artists** — no design; excluded. UPDATE
  2026-09-28: the follow-list data source is now verified — Suno returns
  404 for list endpoints without auth (only counts are public). Profile
  recommendation therefore ships as user-seeded ranking, not crawling:
  `suno-recommend` (#53) scrapes public profile signals (titles, follower
  counts) and ranks candidates via a LAN JEV decision service, with a
  deterministic fallback when no engine is reachable. UPDATE 2026-09-28
  (#54): with `--session-cookie` (env `SUNO_SESSION_COOKIE`) and
  `--expand-following`/`--expand-followers`, seeds may be expanded into
  their follow lists using the user's OWN authenticated session — opt-in
  only, cookie in request headers only, never logged; the authed response
  shape is still unverified until first live use (the parser tolerates the
  plausible families and warns on stderr if none match). UPDATE 2026-09-28
  (#55): `suno-recommend --taste-from <archive-dir>` (env
  `SUNO_TASTE_FROM`) mines track titles from the existing sync archive
  (recursive walk, m4a/mp4 dedupe, capped at 40) and feeds them into the
  JEV decision state so ranking weights similarity to the user's actual
  library; the engine label gains `+taste`. Live-verified: 40 real titles
  load from `~/suno-archive`; with the JEV host unreachable the command
  degrades cleanly to the heuristic. The deterministic fallback never
  claims taste-awareness.
- **`--audio-only` is currently near-useless** — Suno serves no
  unencrypted audio-only bytes; the flag fails most/all tracks live.
  Documented in README and usage.md; revisit if Suno changes delivery.

## Known risks

- If Suno removes the cdn1 MP4 fallback, the tool stops working — the only
  clear delivery channel is theirs to remove.
- Parsers are exercised against live pages via fixtures; a Suno markup
  change can break parsing silently (the dry-run mode is the cheap canary).

## HA add-on era (2026-09-29)

Architecture decision (user-driven): the downloader lives **on the HA box**
as a local add-on, writing directly to `/media/suno/<profile>/` — no
separate download host, no LAN copies. The `ha-suno-archive` custom
component (private repo) was the earlier wrong-layer design and has been
**removed from the HA box** (its repo is parked, kept for the api/puller
test patterns).

Shipped:
- **#67** `suno-webui --ingress` — Host guard off behind HA's ingress proxy.
- **#68** `addon/suno_dl/` — local add-on: config.json (profiles list,
  schedule, audio_only), run.sh (options.json → .env, webui + sync loop),
  Dockerfile (python:3.13-slim + wheel + playwright chromium + ffmpeg).
  Wheel is vendored in the addon dir for the on-box build.
- Add-on **installed and running on the production HAOS host** as `local_suno_dl`
  v1.5.1 (installed via `ha apps install`, built on-box; supervisor's
  local repo path seen from core-ssh is `/local_apps/`).
- `/media/suno/drinko/` holds 171 m4a (~940 MB) — seeded (via the now
  removed component's pull, same target dir).

Tags & Music Assistant (#69, #70):
- **#69** Every downloaded file is now written with embedded `artist`
  (the profile handle) and `title` (the full track title), so MA stops
  showing `[unknown]`. This historical release used mutagen and an ffmpeg
  fallback; the current draft stages Mutagen writes and disables the fallback
  pending metadata-preservation checks. `tagged=N` appears in the summaries.
- **#70** `feat.` truncation had two causes, both fixed: the missing tags
  (now written), and MA treating the first `.` in a basename as the
  extension boundary. New filenames encode mid-name periods as `_`
  (`drinko_Song (feat_ Alice).m4a`). The downloader recognises the old
  period-bearing name for the same track and skips it, so **nothing is
  re-downloaded** — the migration is `suno-dl --retag <dir> --retag-archive-root <root>` (local only,
  idempotent, no renames so MA keeps its indexed paths and play counts).
- Post-merge on the box: rebuild+reinstall the add-on image (it runs a
  vendored wheel), then `suno-dl --retag /media/suno --retag-archive-root /media/suno`, then rescan in Music
  Assistant. Runbook in `addon/suno_dl/DOCS.md`.

Open issue (next agent, priority 1):
- **In-container discovery enumerates only 9 of 172 tracks.**
  `Sync summary: total=9 downloaded=0 existing=9 failed=0` — dedup fine,
  but Suno's anonymous view limits headless-container Chromium to the
  first batch. On a desktop machine with a normal browser the same discovery returned 172. Suspects:
  Chromium launch flags in container (sandbox/webgl), scroll pacing vs
  headless timing, or UA fingerprinting. Repro: `ha apps logs
  local_suno_dl` after a sync; debug with a one-off `docker run` of the
  image running `suno-dl --sync -v`. Do NOT raise concurrency/rate to
  work around it — the politeness charter holds.

Housekeeping notes:
- HA "apps" = add-ons in this HA version (cli: `ha apps …`).
- Supervisor local add-on repo appears at `/local_apps/` from core-ssh.
- The box's Music Assistant is installed but not yet configured —
  filesystem provider should point at `media/suno`.
