# Verified Linux incremental sync

This operational command imports a public profile and all public playlists into
one authoritative library. It only adds missing clip UUIDs and leaves indexed
recordings in place. It does not delete tracks removed from Suno.

Use `suno-verified-sync --library /media/suno --profile drinko --audit /data/archive/verified-sync.jsonl`
to inspect the inventory. Add `--apply` to download. Stop other library writers
first. The command holds Trackie's common archive lock throughout publication.

Each missing clip must have matching canonical URL, song title and producer
metadata in its own Suno document head. Lyrics and React Flight text cannot
supply producer evidence. Filenames use the sanitized producer and song title;
embedded tags and the JSON sidecar retain the original text and creator handle.

Audio is prepared in private staging. Stream-copy extraction must retain the
same audio packet hashes. A targeted tail-moov MP4 edit changes only the artist
and title atoms; unrelated atoms and audio bytes remain untouched by tagging.
Unsupported metadata layouts are refused. No indexed recording is retagged or
replaced by this command.

A durable intent records the staged inodes and hashes before audio and sidecar
publication. Startup rolls an interrupted pair forward only when all existing
destinations are the recorded staged inodes. An unrelated file, altered content,
unowned recording, symlink, conflicting UUID or migration journal stops the run.
Keep the private staging files and intents for recovery; do not delete them to
bypass an error.

This command is Linux only. Its acceptance does not certify the unfinished
general tagging, legacy retag or Windows recovery paths. The separate
[offline migration cutover](producer-migration.md) requires successful terminal
sync acceptance, stopped writers and independent review before any apply.
