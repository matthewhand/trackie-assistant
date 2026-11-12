# Scheduled syncing and Home Assistant clients

The Sync tab controls automatic checks on the authoritative backend. The default
interval is six hours. Change the hours and sources, then select **Save schedule**.
Uncheck automatic syncing and save to pause future checks. An active download
finishes normally. The next scheduled check is measured from the end of the last
sync, and manual checks share the same run manager.

Each profile is checked for public playlists on every run using the anonymous
cursor-based listing behind Suno's **View all playlists** screen. Checks continue
to the terminal page rather than relying on the old profile feed's repeated
playlist subset. If enumeration fails or reaches a safety bound, the UI reports
incomplete discovery and the next run retries it.

Unavailable, private, hidden, deleted, visibility-ambiguous or unsupported song
entries are counted and excluded; their public neighbours can still download.
Every membership page and total must agree. Missing pages, invalid IDs and
malformed entries remain unresolved. A negative visibility result excludes that
clip across the profile and all playlists for that run. Existing local copies
are never removed or changed by these exclusions.
Playlist membership is revalidated even when its title and track count have not
changed. One Suno clip ID has one local recording across the entire destination.
Existing recordings, filenames, embedded tags and cover files are reused unchanged.
Removing a clip from Suno never deletes the local copy.

New recordings use their own verified producer identity. Audio and cover images
are saved; retained videos, retagging, replacement and rotation are not enabled
by the WebUI scheduler. Transfers retain playback-speed pacing and disk reserves.

## Standalone backend

Start the WebUI with `--scheduler`, a writable archive and the intended destination
in `SUNO_OUTPUT_DIR`. Scheduling is disabled until explicitly saved as enabled.
Only one scheduler can own an archive. Saved settings and the last run survive
restarts. A restart during a run retains the retry deadline; the existing download
intent and UUID checks govern recovery.

Bind the backend to loopback behind an authenticated TLS gateway. An exposed
server must also use the WebUI gateway authentication settings. Read-only
gateway processes must not enable the scheduler.

## Home Assistant using that backend

Choose **remote** for `backend_mode` in the add-on options. Configure its HTTPS
backend URL, username and password, the SHA256 fingerprint of the backend's DER
certificate, and the Home Assistant frontend origin (scheme, host and port only).

In remote mode the add-on is an ingress client: it creates no local archive,
starts no local download loop, and sends no backend credentials to the browser.
All scheduling and downloads happen on the backend. Open Sync in the add-on UI
to change or pause its schedule. Local profile/sync options do not run in this
mode. Changing the Home Assistant address requires updating `frontend_origin`.

The backend certificate is pinned before sending authentication. Replacing that
certificate requires an operator to update the pin; mismatches fail closed.
