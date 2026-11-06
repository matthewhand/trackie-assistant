# Diagram inventory — suno-download-cli

Derived from the repository at `ab3b23a` (master). Every component name below is
taken from actual source files; nothing is invented.

**Status: all 10 candidate diagrams delivered.**

## What the project is

A Python CLI that downloads **public** Suno tracks (profile / playlist / song
URLs) for personal archival. No authentication or cookies. Playlist membership has
one narrowly scoped anonymous GET exception for the exact Suno public playlist
metadata endpoint; no other API paths or methods are used.

- Entry point: `suno-dl` → `suno_dl.cli:main` (`pyproject.toml`)
- ~1,675 LOC across 12 modules in `src/suno_dl/`
- 208 tests, ~95% coverage (downloader.py at 99% statements and branches)
- Runtime deps: httpx, beautifulsoup4, python-dotenv, click, pydantic, tqdm,
  playwright

## Proposed diagrams

| # | Filename | Type | Audience | Question it answers | Sources | Confidence |
|---|---|---|---|---|---|---|
| 1 | `01-hero.html` | Architecture (overview) | Everyone | What is this, who uses it, what does it touch? | `cli.py`, `README.md`, `urls.py` | High |
| 2 | `02-architecture.html` | Architecture (zoned) | Developers | How do the modules fit together and what are the boundaries? | all `src/suno_dl/*.py` | High |
| 3 | `03-deployment.html` | Deployment | Operators | Where does this actually run? | `Makefile`, `scripts/ci-local.sh`, `.github/workflows/*` | High |
| 4 | `04-sequence-happy-path.html` | Sequence | Developers | What happens, in order, when I run a download? | `cli.py`, `discovery.py`, `downloader.py`, `fetcher.py` | High |
| 5 | `05-sequence-fallback.html` | Sequence | Developers | What happens when a media URL 403s? | `downloader.py` (`_media_candidates`, `_download_single`) | High |
| 6 | `06-data-flow.html` | Data flow | Developers / security | How does untrusted page content become a safe local file? | `parser.py`, `urls.py`, `naming.py`, `downloader.py` | High |
| 7 | `07-security.html` | Architecture (trust zones) | Security / reviewers | What are the trust boundaries and the 3 defences? | `urls.py`, `naming.py`, `fetcher.py`, `SECURITY.md` | High |
| 8 | `08-state-machine.html` | State machine | Developers | What states can a single track's download be in? | `downloader.py` return literals | High (verified: 3 states) |
| 9 | `09-dependency.html` | Dependency graph | New contributors | Which module imports which? | `grep '^from \.'` across modules | High |
| 10 | `10-operations.html` | Process / flow | Operators | How is it tested and released given no Actions quota? | `scripts/`, `Makefile`, `CHANGELOG.md` | High |

## Assumptions and unknowns (stated explicitly)

- **Playwright discovery path** is only exercised for profiles with `--browser`
  (default on). The sequence diagram shows it as one branch, not both, to stay
  within the complexity budget.
- **CDN hostnames** (`cdn1.suno.ai`, `cdn2.suno.ai`, `*.cloudfront.net`) are
  taken from `urls.ALLOWED_MEDIA_HOSTS` and observed payloads; Suno can change
  them without notice — noted on the security diagram.
- **Signed URL expiry** behaviour is documented in `docs/troubleshooting.md`;
  the exact TTL is *not* known from the repo, so the diagram does not state one.
- **Follower/following lists** exist only as *counts* in the payload (observed:
  `followers_count`, `following_count`). There is **no peer-discovery feature in
  this repo** — no diagram depicts it.
- Diagram 8 (state machine) is derived from the verified `Literal["downloaded",
  "skipped_exists", "failed"]` return type in `_download_single`. There is no
  `no_audio` state — that counter was removed in the hardening pass.

## Excluded (deliberately)

- Radar/bar/scatter charts — no quantitative data in the repo.
- ER/database schema — no database.
- Swimlane / Gantt — single-process CLI, no cross-team handoffs.
- Anything depicting CLM/peer-discovery — not implemented; would be invention.
