# HAOS CT107 dashboard repair

The deployed Suno genre card still used the former standalone backend on port 8760, which is stopped.
It displayed 316 historical entries and could not reach audio. Its separate
Keep-on-HA selection API targets the former copy-based architecture.

The HA-only bridge uses a dated, read-only inventory of CT107's existing library
and Home Assistant's existing read-only `trackie_ct107` media mount. It resolves
signed media URLs through the logged-in HA session on an explicit play click.
Playback is local browser audio; no player service is called. No media is copied,
renamed, retagged, downloaded, or removed. No sync service is started.

The view is intentionally read-only: genre editing and Keep-on-HA operations are
disabled here. Existing genre assignments are retained where the recording exists.
Stored producer metadata is displayed as-is; this does not perform or certify the
separate producer migration. A visible timestamp identifies the snapshot, which
does not automatically refresh after a future sync.

## Prepare and verify on the development host

From the canonical checkout:

```sh
.venv/bin/python deploy/haos-dashboard/prepare.py --ct-host root@hypervisor --ha-host root@home-assistant --out /absolute/new/staging-directory
.venv/bin/python deploy/haos-dashboard/check_bridge.py
.venv/bin/python deploy/haos-dashboard/check_bridge.py --installed-assets /absolute/new/staging-directory
```

Preparation uses SSH to read CT107's sidecars and regular audio-file metadata and
HA's existing card and genre data. It writes only the named staging directory.
It rejects duplicate clip identities, unsafe saved paths and symlinks. Export runs
with CT107 host Python 3.10. Browser checks use installed Google Chrome and fake
audio; they never play music or contact Suno.

For a refresh, use a fresh staging directory. The bridge suffix is replaced rather
than appended again. Publish bridge, snapshot and card last, using temporary files
and atomic renames within `/config/www/suno-genres`. Bump the card's Lovelace
resource query using HA's authenticated resource-update API. Do not edit active
Lovelace storage files directly or restart HA.

## Deployed acceptance, 5 November 2026

- 806 unique audio recordings indexed; original card had 316 entries.
- Actual deployed assets load without console errors, old endpoint requests or POSTs.
- HA media resolution and a 32-byte range read succeed (206, audio/mp4); no playback.
- Offline browser checks cover read-only controls, local audio resolution, pause,
  out-of-order responses, traversal and origin rejection, and disconnect safety.
- This patch does not change the CLI, downloader, add-on image or CT107 containers.

Local deployment receipts and original assets:
`<operator-state-directory>/haos-dashboard-20261105/`.
Restore `before-suno-genre-card.js` and the original resource URL from that record
to roll back. Restoring the old card also restores its known unreachable backend;
it does not restart any backend. The read-only library mount remains unchanged.

## Sync progress

Install read_sync_status.py at /config/trackie/read_sync_status.py and the
trackie_status.yaml package under /config/packages/. Reload command_line using
its existing HA service; no Home Assistant restart is required. The sensor reads
only /media/trackie_ct107/.library/sync-status.json. It does not start jobs, copy
tracks, invoke Music Assistant playback, or write to the share.
