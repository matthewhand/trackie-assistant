# Anonymous public playlist API fixtures

Captured on 2026-10-31 from the five publicly listed playlists for the Trackie
membership-parser regression. Each fixture is a sanitized subset of one
unauthenticated GET response from:

https://studio-api.prod.suno.com/api/playlist/{uuid}/?page=1

Only playlist identity, public/hidden/trashed flags, page and total counts, and
each clip's UUID, title, entity type and visibility flags are retained.
Audio/image/media URLs, user identifiers, cookies, authorization and all other
response fields are deliberately excluded.

The fixtures are static test data. Tests never access Suno or download media.
