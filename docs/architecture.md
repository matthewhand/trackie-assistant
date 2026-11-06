# Architecture

## Purpose

Download **public** Suno tracks from a profile, playlist, or single song URL,
without authentication, for personal archival.

## Module map

```
src/suno_dl/
├── cli.py         Click entry point: argument parsing, URL validation, exit codes
├── config.py      Config model + .env loading (pydantic)
├── discovery.py   Playwright-driven profile discovery (scroll loop)
├── fetcher.py     Async HTTP client: retries, backoff, rate limiting, headers
├── downloader.py  Concurrent download, fallback chain, atomic writes, resume
├── rotate.py      Archive caps: guarded oldest-first pruning; may refuse or
│                  remain over max_tracks / max_disk_mb (#93)
├── parser.py      RSC/Next.js payload parsing → TrackInfo
├── urls.py        Single source of truth: UUID/host/CDN conventions, allow-list
├── naming.py      Filesystem-safe filename construction
├── ratelimit.py   Per-host minimum-interval limiter
├── errors.py      Exception hierarchy
└── models.py      TrackInfo
```

## Data flow

```
  URL
   │
   ▼
 cli._url_kind ──> profile │ playlist │ song
   │
   ├─ profile + browser ──> discovery (Playwright)  ─┐
   │                                                 │ list[TrackInfo]
   └─ otherwise ──> fetcher.fetch_html ──> parser ───┘
                                                     │
                                                     ▼
                                            downloader.download_tracks
                                                     │
                       ┌─────────────────────────────┼──────────────────────────┐
                       ▼                             ▼                          ▼
              resolve media URL              allow-list check          atomic .part write
              (song page RSC)                (urls.py)                 + skip-if-exists
                       │                             │                          │
                       └──────────────► httpx stream ◄──────────────────────────┘
                                          (rate-limited)
```

## Key design decisions

**Discovery and download are separate.** Profile discovery only yields clip IDs
and titles — it does *not* resolve media URLs. Resolution happens in the
downloader, under its concurrency bound, so each song page is fetched exactly
once and discovery stays bounded.

**Untrusted page content is treated as hostile.** Media URLs are extracted from
Suno's payload, which we do not control. Every URL must pass
`urls.is_allowed_media_url` before it is fetched (SSRF defence), and every
output path is built by `naming` and checked for containment in the output
directory (path traversal defence).

**Filenames are derived, never trusted.** Titles become path components only
after sanitisation: separators stripped, Windows device names neutralised,
length bounded, collisions resolved up front.

**Completed outputs are skipped on subsequent runs.** Downloads stage in
`<name>.<ext>.part` before publication, but interrupted staging files may remain
and are not safe to purge automatically. Do not interpret this as byte-range
resumption of every incomplete transfer.

**Rotation is best-effort, not a quota or atomic transaction.** A planned
prune is refused if it would delete the entire archive; failed unlinks or a
crash during multi-file pruning can also leave the archive above its caps or
with partially updated accounting. Containment and symlink checks are
non-atomic against malicious concurrent path replacement. Destructive
rotation assumes trusted storage and exclusive cooperating writers; leave it
disabled if those conditions are unverified. Inventory orphan staging files
read-only, then seek separate approval before any manual cleanup.

## Why `.m4a`?

Suno's CDN serves `.m4a` (AAC/opus) for most clips; the older
`cdn1.suno.ai/<id>.mp3` convention now returns HTTP 403. The downloader uses
whatever the song page's RSC payload advertises (`media_urls`, `video_url`),
falling back to CDN guesses only as a last resort. No transcoding is performed.

## Signed URLs

Some CDN objects are served with a time-limited `sig=` query parameter. These
expire, so URLs are resolved fresh at download time and never persisted. They are
also redacted from logs (`urls.redact_url`) because they are credentials.
