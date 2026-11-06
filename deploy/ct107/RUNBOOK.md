# CT107 deployment runbook

Version-controlled build, run and rollback for the CT107 Trackie Assistant
Compose project (#173). Everything needed to reproduce the running image now
lives in this directory, so a clean checkout builds the same thing.

## Status of this directory

The files here were reconstructed from the repository and the deployment
tickets (#136, #163, #173) because the host copy was never in version control.
**Reconcile before the next rebuild**, in this order:

1. Compare `compose.yaml` with the live project (`docker compose config` on the
   host) and diff service names, mounts, environment and the gateway bind.
2. Record any host-only setting here — a gateway bind, a TLS proxy service, an
   extra volume — so the next reader does not have to guess.
3. Only then build and `up`. Until step 1 is done, treat this as a candidate
   deployment, not a mirror of production.

What is intentionally **not** here: the TLS certificate, the proxy
configuration in front of the gateway, and any credential. Those stay on the
host; only their *names* appear in `compose.yaml`.

## Why the build is guarded

The previous CT107 image was built from a `python:3.12-slim-bookworm` base and
then installed a wheel declaring `Requires-Python >=3.13`. `pip install` fails,
so the image could not be rebuilt from a clean checkout at all — the blocker
that kept the #163 artefacts undeployed after `make manifest` succeeded.

`Dockerfile` now fails the **build** (not the container at runtime) on:

- a base interpreter that is not 3.13,
- zero or more than one wheel in `incoming/`,
- a wheel whose `suno_dl.__version__` differs from `CLI_VERSION`.

## 1. Pin the base image digest

A tag can be re-pointed; a digest cannot. Resolve and pin it at build time:

```console
docker buildx imagetools inspect cgr.dev/chainguard/wolfi-base:latest --format '{{.Manifest.Digest}}'
```

Keep the result with the release record — `scripts/release_manifest.py` already
records the commit and wheel hash, so adding the base digest there (or next to
it) makes the whole toolchain reproducible:

```console
export RUNTIME_BASE="cgr.dev/chainguard/wolfi-base@sha256:<digest>"
```

The `RUNTIME_BASE` build argument is what `compose.yaml` passes through.

## 2. Build the wheel from a pinned commit

Never build from a dirty checkout. `scripts/release_manifest.py` exports
exactly one commit, builds it with a fixed `SOURCE_DATE_EPOCH` and refuses a
wheel whose members differ from that commit:

```console
python scripts/release_manifest.py --ref <commit> --out dist/release-manifest.json
```

Copy the **single** resulting wheel into `incoming/`:

```console
rm -f deploy/ct107/incoming/*.whl
cp dist/trackie_assistant-*.whl deploy/ct107/incoming/
test "$(find deploy/ct107/incoming -maxdepth 1 -name '*.whl' | wc -l)" -eq 1
```

The wheel is ignored by git on purpose: it is a build artefact, and its identity
is recorded by hash in the manifest.

## 3. Preflight

```console
df -h /data                     # the media volume needs headroom for one build
docker system df
docker compose -f deploy/ct107/compose.yaml config -q
```

`/data` is **`backup=0`**. Nothing in this runbook is destructive, but treat a
volume change, a `down -v`, or a media path edit as irreversible: verify the
paths resolve to the existing library before applying them, and never point
`TRACKIE_MEDIA_DIR` at a fresh directory when the old one holds the archive.

## 4. Build and smoke-test in a throwaway container

Build without starting anything:

```console
docker compose -f deploy/ct107/compose.yaml build
```

A build that succeeds can still be an image that cannot start, so prove the
runtime in a container whose only job is to answer questions:

```console
docker run --rm --entrypoint sh trackie-assistant:1.6.1-ct107-source-candidate -ec '
  python -c "import sys; print(sys.version.split()[0])"
  suno-dl --version
  suno-dl --help >/dev/null
  python -c "import suno_dl, pathlib; print(pathlib.Path(suno_dl.__file__).parent)"
'
```

Expect Python `3.13.x`, `suno-dl, version 1.5.3` (the `CLI_VERSION` passed at
build time), and a `suno_dl` package inside the image — not a host path. A
missing `suno-dl` binary, a 3.12 interpreter, or a version mismatch means the
build guards were bypassed; do not deploy it.

Probe the health scripts themselves, since a bad probe is worse than none:

```console
# gateway probe against nothing must fail (exit 1)
docker run --rm trackie-assistant:1.6.1-ct107-source-candidate ct107-healthcheck gateway; echo "exit=$?"
```

## 5. Bring it up

```console
docker compose -f deploy/ct107/compose.yaml up -d
docker compose -f deploy/ct107/compose.yaml ps
```

Both services must report `healthy`. If the worker reports `starting`, read its
logs before touching anything:

```console
docker compose -f deploy/ct107/compose.yaml logs --tail=50 worker
```

`sync disabled (TRACKIE_REAL_SYNC_APPROVED=0); standby` is the **intended**
state, not a fault (#136). The worker keeps its heartbeat fresh in standby, so
`healthy` with sync disabled is correct and complete.

Real syncing is a separate decision. When (and only when) it is approved:

```console
TRACKIE_REAL_SYNC_APPROVED=1 docker compose -f deploy/ct107/compose.yaml up -d worker
```

Leave rotation off. `SUNO_ROTATE` and the `--rotate` flag stay out of this
deployment until #144/#145 are signed off; the worker never passes `--rotate`.

### TLS proxy contract

The gateway listens on `0.0.0.0:8760` **inside** its container and is published
on loopback only, so the TLS proxy is the only network-facing listener. Two
things the proxy must do:

1. **Rewrite `Host` to `localhost`.** The WebUI keeps its DNS-rebinding guard
   on, and it only accepts loopback names plus this machine's own addresses.
   The public DNS name is neither, so passing it through produces a 403.

   ```nginx
   proxy_set_header Host localhost;
   ```

   ```caddyfile
   reverse_proxy 127.0.0.1:8760 {
       header_up Host localhost
   }
   ```

2. **Never expose port 8760 directly.** Plain HTTP to the gateway bypasses TLS
   and the proxy's own access control.

`TRACKIE_GATEWAY_INGRESS` must stay `0`. That flag is Home Assistant-specific:
it resolves the Supervisor's Docker DNS name and then refuses every other peer,
so on a plain Compose host the gateway exits at startup with a name-resolution
error. It is not a way to disable the Host guard here — the header rewrite is.

Verify the gateway over its real address (through TLS, not loopback):

```console
curl -fsS -o /dev/null -w '%{http_code}\n' https://<gateway-host>/api/status
```

`401` is a valid answer when `TRACKIE_GATEWAY_REQUIRE_AUTH=1`: the listener is
up and refusing an unauthenticated read, which is the policy (#162).

## 6. Rollback by retag

Rollback is an image swap, not a rebuild: rebuilding to go backwards would
rebuild the thing you are trying to leave. Keep the previous known-good image
under a stable tag at every deployment:

```console
# Before deploying a new image, preserve the one that is running.
docker tag "$(docker inspect -f '{{.Image}}' \
  "$(docker compose -f deploy/ct107/compose.yaml ps -q gateway)")" \
  trackie-assistant:ct107-rollback
```

To roll back:

```console
docker tag trackie-assistant:ct107-rollback \
  trackie-assistant:1.6.1-ct107-source-candidate
docker compose -f deploy/ct107/compose.yaml up -d --no-build
docker compose -f deploy/ct107/compose.yaml ps
```

`--no-build` is the point: it refuses to quietly rebuild the tag you just
re-pointed. Confirm the CLI version in the logs afterwards
(`docker compose logs gateway | grep "cli:"`), and confirm the media inventory
is unchanged:

```console
find "${TRACKIE_MEDIA_DIR:-/data/trackie-assistant/media}" -name '*.m4a' | wc -l
```

Rollback never touches media. If that count moved during a deployment, stop and
investigate before doing anything else.

## 7. What not to do

- Do not `docker compose down -v`; the volumes are the archive and the library.
- Do not recreate the gateway to "fix" a health status without reading its
  logs — a policy refusal is reported as `unhealthy` only when the listener is
  genuinely down.
- Do not run a second sync alongside the worker. The archive lock serialises
  cooperating writers; unrelated writers on the same media are unsupported
  until #145 is qualified.
- Do not delete `*.part` staging files to reclaim space. Inventory them first;
  automatic scavenging is deliberately disabled.

Image builds require an explicit `CLI_VERSION` matching the wheel and
`TRACKIE_IMAGE` identifying the reviewed image. No old release is selected
by a Compose default. Both image recipes accept a digest-pinned Wolfi base.


### Maintained runtime packages

The candidate uses Wolfi's glibc-based runtime, Python 3.13 in an isolated
virtual environment, and the maintained FFmpeg 9 package family. Pin
RUNTIME_BASE to the reviewed Wolfi digest. Record the resolved APK/Python
inventories and scan the final image; pinning the base alone does not freeze
the rolling package repository. The optional browser uses Playwright's matched
headless build under /opt/playwright, readable by UID 10001. Its unused
video-recording FFmpeg helper is removed; Trackie uses the APK-managed media tools.
