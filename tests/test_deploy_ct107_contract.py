"""CT107 deployment contract (#173).

The deployment used to exist only on the host, so a clean checkout could not
reproduce the running image and its Dockerfile drifted onto a Python 3.12 base
that the `>=3.13` wheel refuses to install into. These are the offline checks
that keep the committed files honest: no Docker daemon, no network.
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "ct107"
DOCKERFILE = DEPLOY / "Dockerfile"
COMPOSE = DEPLOY / "compose.yaml"
RUNBOOK = DEPLOY / "RUNBOOK.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deployment_artifacts_are_committed() -> None:
    for name in (
        "Dockerfile",
        "compose.yaml",
        "RUNBOOK.md",
        "gateway-entrypoint.sh",
        "worker-entrypoint.sh",
        "healthcheck.py",
    ):
        assert (DEPLOY / name).is_file(), f"deploy/ct107/{name} is missing"


def test_entrypoints_are_executable() -> None:
    for name in ("gateway-entrypoint.sh", "worker-entrypoint.sh", "healthcheck.py"):
        path = DEPLOY / name
        if sys.platform == "win32":
            # Windows checkout permissions cannot represent Git's executable bit.
            entry = subprocess.run(
                ["git", "ls-files", "--stage", "--", path.relative_to(ROOT).as_posix()],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert entry.startswith("100755 "), f"deploy/ct107/{name} is not executable in Git"
        else:
            assert path.stat().st_mode & stat.S_IXUSR, f"deploy/ct107/{name} is not executable"


def test_base_image_is_python_313_and_digest_pinnable() -> None:
    dockerfile = _text(DOCKERFILE)
    # The base is an ARG so a real build pins a digest, not a moving tag.
    assert re.search(r"^ARG RUNTIME_BASE=cgr\.dev/chainguard/wolfi-base:latest$", dockerfile, re.M)
    assert "FROM ${RUNTIME_BASE}" in dockerfile
    assert "python-3.13" in dockerfile and "ffmpeg-9.0" in dockerfile
    assert "python:3.12" not in dockerfile
    # A wrong base must fail the build, not the container at runtime.
    assert "assert sys.version_info[:2] == (3, 13)" in dockerfile


def test_build_refuses_zero_or_multiple_wheels_and_a_version_mismatch() -> None:
    dockerfile = _text(DOCKERFILE)
    assert "COPY incoming/ /tmp/incoming/" in dockerfile
    assert 'if [ "$wheels" -ne 1 ]' in dockerfile
    assert "does not match CLI_VERSION" in dockerfile
    assert (DEPLOY / "incoming" / ".gitkeep").is_file()
    assert "*.whl" in _text(DEPLOY / ".gitignore")  # wheels are artefacts, not source


def test_both_services_declare_a_docker_healthcheck() -> None:
    compose = _text(COMPOSE)
    services = compose.split("\nservices:", 1)[1]
    gateway, worker = services.split("\n  worker:", 1)
    for block, name in ((gateway, "gateway"), (worker, "worker")):
        assert "healthcheck:" in block, f"{name} has no healthcheck"
        assert f'["CMD", "ct107-healthcheck", "{name}"]' in block


def test_worker_sync_stays_behind_the_approval_gate() -> None:
    compose = _text(COMPOSE)
    entrypoint = _text(DEPLOY / "worker-entrypoint.sh")
    assert "TRACKIE_REAL_SYNC_APPROVED" in compose
    assert "TRACKIE_REAL_SYNC_APPROVED" in entrypoint
    # Disabled sync must be a documented standby, not an error path.
    assert "standby" in entrypoint
    # Rotation is a separate, unqualified decision for this host (#144/#145):
    # no non-comment line may hand --rotate to the CLI.
    rotate_lines = [
        line
        for line in entrypoint.splitlines()
        if "--rotate" in line and not line.lstrip().startswith("#")
    ]
    assert rotate_lines == []
    assert "SUNO_ROTATE" not in compose


def test_gateway_does_not_claim_home_assistant_ingress() -> None:
    """`--ingress` resolves the Supervisor name and refuses every other peer.

    On a plain Compose host it cannot even bind, so the default must stay off
    and the runbook must document the proxy's Host rewrite instead.
    """
    compose = _text(COMPOSE)
    entrypoint = _text(DEPLOY / "gateway-entrypoint.sh")
    assert "TRACKIE_GATEWAY_INGRESS: ${TRACKIE_GATEWAY_INGRESS:-0}" in compose
    assert "${TRACKIE_GATEWAY_INGRESS:-0}" in entrypoint
    assert "proxy_set_header Host localhost" in _text(RUNBOOK)
    assert 'export SUNO_OUTPUT_DIR="$MEDIA"' in entrypoint


def test_runtime_is_least_privilege_and_does_not_publish_the_gateway() -> None:
    compose = _text(COMPOSE)
    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    # Loopback only; the TLS proxy is the network-facing listener.
    assert (
        '"${TRACKIE_GATEWAY_HOST_IP:-127.0.0.1}:${TRACKIE_GATEWAY_HOST_PORT:-8760}:8760"' in compose
    )
    # The gateway never gets a writable view of the media library.
    assert "${TRACKIE_MEDIA_DIR:-/data/trackie-assistant/media}:/media/suno:ro" in compose


def test_compose_commits_no_credentials() -> None:
    compose = _text(COMPOSE)
    assert "TRACKIE_GATEWAY_USER" not in compose
    assert "TRACKIE_GATEWAY_PASSWORD" not in compose
    assert "password" not in compose.lower()


def test_health_probe_treats_a_policy_refusal_as_healthy() -> None:
    probe = _text(DEPLOY / "healthcheck.py")
    # 401/403 from the auth policy means the listener is up (#162).
    assert "HTTPConnection" in probe
    assert "status >= 500" in probe
    # A disabled-sync standby still beats, so an old beat is a stuck loop.
    assert "worker heartbeat" in probe


@pytest.mark.parametrize(
    "required",
    [
        "Pin the base image digest",
        "throwaway container",
        "compose.yaml up -d",
        "Rollback by retag",
        "clean checkout",
        "backup=0",
        "TRACKIE_REAL_SYNC_APPROVED=0",
    ],
)
def test_runbook_covers_the_acceptance_steps(required: str) -> None:
    assert required in _text(RUNBOOK)


def test_runbook_states_the_reconstruction_gap() -> None:
    # Compare wrapped prose, not raw line breaks: the runbook is hand-wrapped.
    runbook = " ".join(_text(RUNBOOK).split())
    # The host copy was never in version control; readers must reconcile it.
    assert "Reconcile before the next rebuild" in runbook
    assert "candidate deployment, not a mirror of production" in runbook
    # And it warns against the destructive shortcuts that would lose the archive.
    assert "down -v" in runbook
    assert "automatic scavenging is deliberately disabled" in runbook
