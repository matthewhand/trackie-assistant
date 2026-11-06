# Selected playlists and published media

User-approved Linux mode: audio plus embedded artwork and a separate image.
Temporary video sources are allowed for audio extraction or missing artwork.
The published MP4 is discarded after verification; audio is stream-copied,
not re-encoded. No legacy CDN address is manufactured by the verified engine.

Each producer is verified from the canonical clip document head. Asset URLs
must be explicitly published in that page and match the exact clip UUID on
observed official CDN routes. Encoded website streams are skipped rather than
decrypted or passed to the decoder. Refusals and throttling stop the operational
run; unavailable identities are never guessed.

Each selected public playlist's membership is fetched and checked afresh.
UUIDs are deduplicated across playlists and the entire existing library.
Indexed recordings are reused unchanged. Existing-library filename, artist
and artwork repairs require a separate stopped-writer cutover.

The profile endpoint currently repeats a window of 16 playlists. The app now
reports repeated or capped discovery as incomplete and asks for explicit
playlist URLs. It must not claim the entire profile is synchronized from that
window. Explicit CLI --watch entries replace the configured watch list.

The browser Sync panel accepts explicit source URLs, has a preview action,
and explains cover handling, temporary video use and playback pacing.
The HA card reads a small CT107 status snapshot through a read-only command-line
sensor. It shows selected playlists, existing/new/missing counts, paced waiting,
failures and last-update time. Stale progress is explicitly unconfirmed.
The library browser snapshot has its own distinct date.

Publication remains the tested staged group protocol. The status file is only
a UI snapshot, not a recovery authority. The pacing checkpoint uses same-boot
monotonic deadlines and waits conservatively after a reboot. A fresh process
must not bypass the preceding recording's reserved interval.

All development and packaging run on the designated Linux development host. No push, hosted CI or merge
is part of this local rollout. The recurring routine remains paused.
