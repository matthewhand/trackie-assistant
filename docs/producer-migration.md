# Producer filename and artist migration: Linux cutover

This is an operator-run, offline migration candidate, not a general release.
It changes older filenames and the embedded artist using each clip's own
verified producer. Profile membership is source provenance, never an artist.
It does not fetch audio, delete recordings removed from Suno, or create a second
copy of an indexed clip.

## Preconditions for CT107

1. Keep the exact incremental sync running unchanged. Do not collect competing
   Suno evidence, install a new runtime, retag files or invoke migration while it
   runs.
2. Confirm the authoritative container inspection and its audit belong to the
   selected run. The sync_acceptance.verify_terminal_success function requires
   the exact full container ID, exited status, exit code zero, one final
   successful completion record and consistent unique-UUID download counts.
   A missing, failed, partial, changed or ambiguous audit is a stop condition.
3. Establish that every other library writer is stopped and stays stopped:
   worker, gateway, HAOS add-on, scheduled/manual syncs, direct metadata helpers
   and external share clients. The common advisory lock coordinates new Trackie
   code; it does not stop an old binary or an unrelated process. Music Assistant
   must access the authoritative library read-only during cutover.
4. Confirm the authoritative root and ownership, all UUID/media claims, no
   duplicate recordings, no pending publication intents, and sufficient space.
   A leftover intent or staging artifact requires recovery investigation.
   Do not delete it to make preflight pass.

These checks are operator/orchestration requirements. A boolean assertion or
a supplied hash alone does not establish that they were performed.

## Producer evidence and independent review

Run "suno-producer-evidence LIBRARY STATE_DIR --fetch --writers-stopped" only
after the preconditions above. Use a new private state directory outside the
music library for the v2 evidence protocol. Collection holds the common archive
lock, caches each bounded HTTPS song page, and is resumable. It verifies the
requested UUID's canonical URL and agreeing head metadata, recording both
producer display name and canonical creator handle. Lyrics, Flight strings and
legacy sidecar artist fields cannot supply identity.

All clips must verify. Review the cached own-clip evidence, the complete
UUID-to-artist and UUID-to-handle mappings, and the exact read-only
plan_migration result independently. Unverified, removed, unsupported or
ambiguous clips stop application. Bind the review to review_plan_digest(plan).
Apply recomputes the plan under the archive lock and refuses drift.

Call public apply_migration from a dedicated synchronous process with
writers_stopped=True, producer_mapping_reviewed=True, matching
reviewed_producers, reviewed_creator_handles, and reviewed_plan_sha256.
Supply the verified creator_handle_by_id mapping.
The public apply/resume functions acquire the lock themselves; do not pre-acquire
it. Do not use legacy --retag as a substitute.

## Preservation and crash recovery

Journal v10 uses the restricted lossless-artist-v1 writer. Supported layouts
are tail-moov audio MP4/M4A, plain ID3v2.4 (or restricted v2.3 with sufficient
existing padding) MP3, and supported RIFF/WAV with a final ID3 chunk. Only the
artist is patched; encoded audio, unrelated supported metadata bytes, title
and other non-target fields are preserved. A decoder check additionally verifies
the audio. Unsupported layouts fail closed before track moves.

Linux inode/fsx flags, extended attributes, sparse allocation, ownership, mode,
same-filesystem staging and available space are checked. The changed audio's
integer mtime differs from the old value so Music Assistant can detect it.
Timestamps other than the intended mtime change are not preserved.

Durable journal and prepared-file identities bind recovery to the exact owned
inodes and hashes. If interrupted, keep every writer stopped. Independently
review the pending journal and the producer provenance, then use public
resume_migration with its exact reviewed journal digest. A prepared file
without its durable ownership record is refused. Older incomplete v6-v9
journals require manual investigation rather than automatic v10 replay.
Never blindly retry an uncertain publication.

After completion, re-inventory UUIDs and media, verify names and artist/handle
claims against the reviewed mapping, verify audio and unrelated metadata
preservation, confirm no duplicate or orphan files and a durable completion
record, then refresh Music Assistant's file source. Resume only writers using
the reviewed producer-aware implementation.

## Release boundary

Current delivery targets Linux on CT107 and HAOS. Windows recovery is future,
unscheduled [roadmap work](ROADMAP.md), and does not block this migration or a
Linux release. Keep unsupported Windows archive writes disabled.

Linux release acceptance still covers the CLI/web/API producer and tag paths,
the final add-on package, and the migration's own safety prerequisites.
