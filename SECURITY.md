# Security Policy

## Scope

Trackie Assistant makes outbound HTTP requests to Suno and its CDN. The most
relevant security property is that **parsed page content is untrusted**: it must
never be able to make the tool write outside the output directory or fetch
internal addresses.

## Supported release boundary

Archive writes in the current candidate target Linux, Python 3.13, FFmpeg and
Mutagen. Indexed recordings are additions-only during normal sync; older-file
repairs require the offline migration and an enforced all-writers-stopped cutover.
A pending or uncertain publication must be investigated through recovery rather
than blindly retried. Do not share a writable library with noncooperating writers.
The local WebUI is not a hardened public internet service; bind it to loopback or
use a trusted authenticated gateway. HA ingress and the read-only media share are
separate from the library writer.

## Design guarantees

- Media URLs are filtered through an allow-list (`suno_dl.urls.is_allowed_media_url`).
  Only `https` URLs on `*.suno.ai`, `*.suno.com`, and `*.cloudfront.net` are fetched.
  IP literals and URLs with embedded credentials are rejected (SSRF defence).
- Output paths are sanitised (`suno_dl.naming`) and checked for containment in the
  output directory before writing.
- Signed URL query parameters are redacted from logs (`suno_dl.urls.redact_url`),
  and the `httpx` logger is pinned to WARNING so it cannot print them.

## Reporting a vulnerability

Please open a private security advisory via the repository's **Security** tab
("Report a vulnerability") rather than a public issue. Include a minimal
reproduction. We aim to acknowledge within a few days.

Do not test against Suno's infrastructure in ways that could be construed as
abuse.


## Publication privacy

A public release must use a reviewed source export with fresh repository history,
not a push or mirror of a private checkout. Do not transfer private branches,
archive refs, tags, reflogs, local configuration, audit logs or download state.
Review extracted wheels and source archives as well as current source; inspect
image metadata and visible screenshots. Secret-pattern tools have false negatives
and false positives. If an actual credential was ever published, revoke it:
removing a commit does not make the credential safe again.
