# Troubleshooting

## `Found 0 public track(s)`

- The profile handle may be wrong, renamed, or private.
- Suno may have changed its markup. Try a direct song URL to confirm the core
  download path still works.
- For profiles, the browser fallback needs the optional `browser` extra and
  Chromium (`pip install 'trackie-assistant[browser]'` then
  `python -m playwright install chromium`). Without it, use `--no-browser`.

## Everything 403s

Some CDN objects are served via **signed, time-limited URLs**. The tool resolves
them fresh at download time, so a re-run usually fixes transient 403s. If 403s
persist across runs, upstream behaviour has changed — please open an issue with
the `-v` output (it is safe to paste: signed URLs are redacted).

## Profile stops at ~20 tracks

Suno limits anonymous views of long profiles. Options:

- Re-run; the virtualised list sometimes loads further.
- Use the profile's individual song URLs.
- Accept the featured subset and archive incrementally.

## `playwright` import or browser errors

Browser fallback is an optional extra; the HTTP feed never needs it.

```bash
pip install 'trackie-assistant[browser]'
python -m playwright install chromium
```

Without a browser you can still use `--no-browser` for song and playlist URLs
(profiles then parse whatever server-rendered anchors exist, which may be a
subset).

## Downloads fail mid-run

- Partial downloads use `*.part` staging files. An interrupted run can leave
  orphan staging files; automatic age-based scavenging is intentionally disabled.
  Do not assume every `.part` is disposable: another writer may still be using
  it. First stop or exclude all writers, inventory exact paths without removing
  anything, and arrange separately authorized cleanup only after ownership and
  filesystem safety are verified.
- Increase the timeout: `SUNO_TIMEOUT_SEC=60`.
- Lower concurrency / add a rate limit (see [usage](usage.md)).
- Re-run — completed tracks are skipped.

## Files land as `.m4a`, not `.mp3`

That is what Suno's CDN serves. The `.m4a` container (AAC/opus) plays in VLC,
Plex, foobar2000, and most players. To transcode for a strict MP3-only device:

```bash
for f in *.m4a; do ffmpeg -i "$f" -codec:a libmp3lame -q:a 2 "${f%.m4a}.mp3"; done
```

## Two tracks overwrote each other

They should not — identical sanitised titles are de-duplicated with a `_2`
suffix. If you see a collision, please report it. Using `{id}` in `--pattern`
makes names unique by construction.

## Windows: `CON`, `NUL`, or trailing-dot filenames

Reserved device names are prefixed with `_` and trailing dots/spaces stripped
automatically. If you still hit an OS error, open an issue with the title.

## Debug output

```bash
suno-dl -v --url ...
```

`httpx` is pinned to WARNING and URLs are redacted, so `-v` output is safe to
share.
