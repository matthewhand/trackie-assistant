# Local CI (manual alternative to GitHub Actions)

GitHub Actions workflows remain in the repository, but hosted CI may be
unavailable due to account limits. On a **Linux CI host**, the manual
`scripts/ci-local.sh` harness reproduces test/build gates in
`.github/workflows/ci.yml` using a **fresh disposable clone**.

**Warning:** The harness deliberately deletes and recreates its dedicated CI
scratch clone on each run. Do **not** point it at a working checkout, a mounted
directory, or user data. Guards fail closed on detected unsafe paths, but
cannot make a destructive reset atomic against a malicious same-user process.

| CI job | Local equivalent |
| --- | --- |
| Python 3.13 | One venv and `uv pip install -e ".[dev]"` |
| `ruff check` | `ruff check src tests` |
| `ruff format --check` | `ruff format --check src tests` |
| `mypy` | `mypy src` |
| `pytest --cov --cov-fail-under=70` | The same coverage floor |
| Build and wheel smoke | `python -m build --no-isolation`, clean-venv wheel install, CLI `--version` / `--help` |

The application's supported Python version is **3.13**, also tested on
Windows and macOS in hosted CI. Only this *manual CI harness and its Linux
mount checks* are Linux-specific; the add-on image is based on 3.13 too.

## Requirements and run

The Linux harness needs Bash, Python 3, `/proc/self/mountinfo`,
util-linux `flock`, GNU `rm`, Git, and `uv`
on `PATH`. The default `CI_PYTHONS` matrix is `3.13`; a missing interpreter is
reported as **SKIP**, not counted as verified. Build/smoke also defaults to
Python 3.13.

After confirming the dedicated scratch directory contains **nothing you need
to keep**, invoke the script only with appropriate operational approval:

```bash
bash scripts/ci-local.sh hardening/security-and-safety
```

The argument selects the remote Git branch/ref for the fresh clone (default
`master`). Running the harness is **not** a read-only synthetic
check: it resets its scratch clone and installs CI dependencies there.

## Dedicated workspace, logs and locking

All refs share **one** dedicated scratch clone to prevent overlapping manual
CI invocations from overwriting each other's files:

| Purpose | Default path |
| --- | --- |
| Scratch clone (**deleted and recreated**) | `~/.local/state/suno-ci-local/work` |
| Logs and `summary.txt` (**preserved**) | `~/.local/state/suno-ci-local/logs` |
| Global lockfile (**preserved**) | `~/.local/state/suno-ci-local/.ci-local.lock` |

The `CI_WORKDIR` override is accepted **only** if it is the
exact dedicated scratch path; any other value fails with exit code **78**.
Changing the ref does not change the workspace. The script rejects symlinked
existing workspace ancestors and an existing non-directory workspace.

`CI_LOGDIR` may be overridden only with an **absolute,
unambiguous path outside** the scratch clone. Empty path components,
`.`, `..`, symlinks (including parent symlinks)
and non-directory parents are rejected. The log guard runs **before and
inside the lock**. Detailed logs use UTC-stamped `run-*.log`
files; summaries are appended to `summary.txt`. Keep logs
outside `work`, which gets deleted.

A global nonblocking `flock -n -o -E 75` prevents overlapping
runs, even with different refs. **Exit code 75** means another run holds the
lock; retry later. A caller cannot bypass locking by setting
`SUNO_CI_LOCKED`. The `-o` flag closes the lock
descriptor in the worker, preventing spawned descendants from extending the
supervisor's lock lifetime. Do not delete or replace the lockfile while a
run is active.

## Mount and filesystem safety

The mount guard reads **every** record in `/proc/self/mountinfo`,
including nested mountpoints beneath a normal scratch directory. It rejects
mounts **at or below** the workspace and fails closed if mountinfo is missing,
unreadable or malformed. The check occurs before locking and **again while
holding the lock**, immediately before reset. The permitted destructive
command is `rm -rf --one-file-system -- "$WORK"`, restricted
to the exact dedicated scratch path. Log-path checking also runs twice.

Unsafe scratch/log paths fail with **78**; missing required prerequisites or
unreadable mount information stop the harness (typically **4**). They are not
quietly skipped. No privileged mount operation is part of the helper.

**Limitations:** These are pathname **preflight checks**, not atomic
confinement. Concurrent same-user pathname swaps or mount-namespace changes
may occur between the check and deletion; noncooperating processes do not
honor the global lock. Use a trusted single-user CI host, avoid root, and
never put valuable data under the scratch path. This safety boundary applies
to the **manual CI checkout**, not production music archives.

## Scheduled local CI (optional)

An explicitly enabled systemd **user** timer can run the manual harness every
three hours. Reading its state is non-disruptive:

```bash
systemctl --user status suno-ci-local.timer
systemctl --user list-timers suno-ci-local.timer
journalctl --user -u suno-ci-local.service -n 50
cat ~/.local/state/suno-ci-local/logs/summary.txt
```

Units live at `~/.config/systemd/user/suno-ci-local.{service,timer}`.
**Enabling the timer schedules recurring destructive scratch resets** and
requires separate operational approval. For reference, the commands are:

```bash
systemctl --user daemon-reload
systemctl --user enable --now suno-ci-local.timer
```

Running the timer after logout requires administrator approval to enable
lingering:

```bash
sudo loginctl enable-linger "$USER"
```

Without lingering, the user timer runs only while a user session is
available. To disable an existing schedule:

```bash
systemctl --user disable --now suno-ci-local.timer
```

# Release manifest (pinned, reproducible wheel)

`scripts/release_manifest.py` answers "which commit is this wheel, exactly?"
without trusting a version string — the lesson from a container image and a
GitHub package both reporting `1.5.3` while containing different bytes.

```bash
python scripts/release_manifest.py                    # manifest for HEAD on stdout
python scripts/release_manifest.py --ref b892424      # any pinned ref
python scripts/release_manifest.py --verify-wheel dist/package.whl  # compare exact bytes
make manifest REF=b892424                             # -> dist/release-manifest.json
```

It exports the ref twice with `git archive` (never the working tree), builds
two wheels with `SOURCE_DATE_EPOCH` set to the commit timestamp, and refuses
nonidentical builds. It checks every shipped `suno_dl/` member against the
committed file, validates the wheel RECORD, and emits JSON containing:

- `commit.sha` / `commit.committed_epoch` — the exact source tree
- `wheel.sha256`, `wheel.size`, and a `wheel.members` SHA-256 map
- `package` — name, version, `Requires-Python`, `Requires-Dist`, entry points
- `toolchain` — Python, `build`, `setuptools`, `wheel` versions

Use `--verify-wheel` to compare an existing wheel byte-for-byte with the two
verified builds. Without this option the manifest describes the temporary build,
not an independently produced or published wheel. Reproducibility is checked
with the recorded local toolchain; a different toolchain may produce different
bytes. The release workflow verifies the wheel it uploads and includes the
manifest alongside it. The manifest does not attest the source distribution.

Exit codes: `0` manifest written, `1` the artefact does not match the commit
(stale member, missing member, corrupt zip), `2` missing `git`/`tar`/`build`
tooling or an unresolvable ref.
