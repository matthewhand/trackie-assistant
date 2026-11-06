# Release verification runbook (Actions-budget outage)

## Current private-repository acceptance policy (2026-10-29)

For this private repository, hosted GitHub Actions is **non-blocking** by
owner decision. The historical outage investigation below remains for
reference, not as a requirement to rerun failed Actions jobs before merging.
Every proposed source or release change still requires independent review,
actual Python 3.13 offline tests, Ruff, MyPy, deterministic wheel/package
checks and normal GitHub merge safeguards as applicable. Do not bypass branch
protection or assert that a release was verified without a real artifact smoke
test. Local acceptance is not proof that any hosted job ran successfully.

### Archive and staging safety for release operators

`--no-rotate` is the default and performs **no rotation deletion**. With
`--rotate`, oldest-first pruning may refuse an all-track removal or remain
over caps after incomplete scanning, failed unlink or interruption. Pruning
multiple media files is not atomic: accounting can disagree with files after
a crash. Never infer that a cap is an absolute filesystem quota.

A failed or killed download may retain orphan `*.part` staging files. Do not
auto-scavenge by age or assume they are unowned. First perform a read-only
inventory of exact paths, size, timestamps and writer evidence (without
reading private media contents). Preserve uncertain entries, and obtain
separate destructive approval before any supervised cleanup.

Path, symlink and inode checks cannot guarantee safe unlink against hostile
concurrent directory mutation. Destructive rotation requires storage-specific
trust and exclusive cooperating writers; if those cannot be demonstrated,
keep it disabled. Before release acceptance, record a read-only dry-run,
interrupted/partial-prune caveats, preserved media and rollback limits.

---

## Historical Actions-budget context

Historically, GitHub Actions runs on this account were blocked by an
Actions budget restriction (reset originally estimated ~2026-10-01).
Budget-blocked runs failed before starting steps with the annotation:

> The job was not started because an Actions budget is preventing further use.

**This is not a code failure.** How to tell the difference:

| Signal | Budget failure | Real failure |
|---|---|---|
| Duration | 1–7 s | minutes |
| Annotation | "Actions budget is preventing further use" | test/lint/build error |
| Jobs | none started (`-` or `X` immediately) | some jobs actually ran |
| Local CI (`scripts/ci-local.sh`) | GREEN | usually RED too |

The authoritative gate during the outage is the local harness, which runs the
identical steps as CI (same matrix, same commands):

```bash
bash scripts/ci-local.sh <branch-or-master>
```

## What exists right now

- Tag `v1.0.0` (annotated) → commit `fc03768`
- GitHub release `v1.0.0` with sdist + wheel attached, built and verified
  locally with the same steps release.yml runs (`python -m build`, `twine
  check`, clean-venv wheel install, `suno-dl --version` smoke)
- Tag `v1.1.0` (annotated) → commit `55772bf`; GitHub release with sdist +
  wheel attached, built and verified locally the same way.
- Tag `v1.1.1` (annotated) → commit `0770d2f`; GitHub release with sdist +
  wheel attached, built and verified locally the same way. Ships the
  critical media-payload validation fix (#40).
- Release workflow runs: `36296629295` (v1.0.0), `36305586532` (v1.1.0),
  `36307505930` (v1.1.1), `36363758083` (v1.2.0), `36480507958` (v1.3.0),
  `36491794003` (v1.4.0), `36495941398` (v1.5.0) — each failed on the
  budget annotation (v1.1.0's also confirmed failed on a manual rerun,
  attempt 2). All workflow configs are fine; each needs one `gh run rerun`
  after the budget resets. The v1.3.0 release itself was created locally
  (tag push → 7s budget failure → local build, twine check, clean-venv
  wheel smoke, `gh release create` with both artifacts attached). The
  v1.4.0 release followed the same local path (20s probe showed all six
  `test` matrix jobs failing with 0 steps — the budget signature — while
  `build`/`github-release` were skipped).

## When the budget resets

### 1. Confirm Actions actually works again

```bash
gh run list --limit 3        # any recent run that actually executed jobs?
```

### 2. Verify the tagged commit passes the new release gate

One command reruns and classifies every budget-blocked run (release runs
plus any CI runs from merges made during the outage):

```bash
bash scripts/verify-releases.sh
```

It reports each run as GREEN, EXECUTED AND FAILED (real failure — check
`gh run view <id> --log-failed`), or BUDGET-STILL-BLOCKED (0 steps
executed — budget not reset yet; exit code 2). Or rerun individually:

```bash
gh run rerun 36296629295   # v1.0.0
gh run rerun 36305586532   # v1.1.0
gh run rerun 36307505930   # v1.1.1
gh run rerun 36363758083   # v1.2.0
gh run rerun 36480507958   # v1.3.0
gh run rerun 36491794003   # v1.4.0
gh run rerun 36495941398   # v1.5.0
gh run watch 36480507958 --exit-status
```

Expected: the `test` job (lint/format/mypy/pytest matrix) runs first, then
`build` (tag==version check, sdist+wheel, `twine check`, artifact upload),
then `github-release`. If the release already exists (it does — created
locally), `softprops/action-gh-release` updates the existing release and
re-uploads the assets rather than failing.

### 3. Decide: rerun is enough, or re-tag?

- **Rerun green** → done. The release's provenance now includes an Actions-
  built artifact set; the locally built assets are replaced (same content,
  same commit).
- **Workflow config problem** → fix on a branch via PR, merge, then
  re-tag (`git tag -d v1.0.0 && git push origin :refs/tags/v1.0.0` is only
  acceptable BEFORE anyone depends on the release; prefer a `v1.0.1`).

### 4. Verify CI on master too

```bash
gh run rerun <latest-failed-master-run>
```

or simply push any commit; master's CI runs on push.

## If something is red for a real reason

1. Read the failed job log: `gh run view <id> --log-failed`
2. Reproduce locally: `bash scripts/ci-local.sh master` (or the relevant ref)
3. Fix on a branch through a PR; never push a new tag until master is green

## Why the local harness is authoritative

`scripts/ci-local.sh` reproduces every CI step against a **pristine clone**
of the repo: Python 3.13 matrix, `ruff check`, `ruff format --check`,
`mypy src`, `pytest --cov-fail-under=70`, then `python -m build` + wheel
install in a clean venv + `suno-dl --version`/`--help` smoke. It exits
non-zero on the first real failure and appends a `RESULT: GREEN/RED` line to
`~/.local/state/suno-ci-local/logs/summary.txt`.

## Notes

- The failing CI check on commit `fc03768` (and the tag) is **expected** to
  stay red until the budget resets; do not "fix" it.
- PyPI publishing remains intentionally disabled (commented out) in
  `.github/workflows/release.yml`; the GitHub release is the distribution
  channel for now.
