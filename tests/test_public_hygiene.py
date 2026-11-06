"""Public-release hygiene: no private addresses or site-specific hosts (#107).

This repository ships publicly, so tracked text must not name a personal
machine or a LAN/tailnet address. ``tests/test_urls.py`` deliberately keeps one
private-range URL (the SSRF allow-list regression); it is allow-listed below so
that a *different* private address in that file still fails the scan.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"

# RFC1918 (10/8, 172.16/12, 192.168/16) plus CGNAT/tailnet (100.64/10).
PRIVATE_ADDRESS = re.compile(
    r"(?<![\d.])("
    rf"10\.{_OCTET}\.{_OCTET}\.{_OCTET}"
    rf"|192\.168\.{_OCTET}\.{_OCTET}"
    rf"|172\.(?:1[6-9]|2[0-9]|3[01])\.{_OCTET}\.{_OCTET}"
    rf"|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.{_OCTET}\.{_OCTET}"
    r")(?![\d.])"
)

# Named development machines (``ubuntu-<name>``; GitHub's ``ubuntu-latest`` and
# ``ubuntu-24.04`` runner images are infrastructure, not a personal host),
# mDNS names and Tailscale MagicDNS names. Assembled from fragments on purpose
# so this file does not flag itself.
SITE_HOST = re.compile(
    r"(?<![\w.-])ubuntu-(?!latest(?:[\w-]*)|\d)[a-z0-9][a-z0-9-]*"
    r"|(?<![\w.-])windows[12](?![\w.-])"
    r"|(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+local(?![\w.-])"
    r"|(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+ts\.net(?![\w.-])",
    re.IGNORECASE,
)

# (repo-relative path, matched address) pairs that are intentional fixtures.
_ALLOWED_ADDRESSES = {
    ("tests/test_urls.py", ".".join(("10", "0", "0", "1"))),
}


def _tracked_text_files() -> list[tuple[str, str]]:
    """Tracked, non-binary files as (repo-relative path, decoded text)."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - no checkout
        pytest.skip(f"tracked-file scan needs a git checkout: {exc}")

    files: list[tuple[str, str]] = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        path = ROOT / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:  # wheel, image, archive
            continue
        files.append((relative, data.decode("utf-8", "replace")))
    return files


def test_tracked_files_contain_no_private_infrastructure() -> None:
    """One inventory, both privacy rules, all tracked text including deployment docs."""
    found = []
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, allowed in (
                (PRIVATE_ADDRESS, _ALLOWED_ADDRESSES),
                (SITE_HOST, frozenset()),
            ):
                for match in pattern.finditer(line):
                    if (relative, match.group(0)) not in allowed:
                        found.append(f"{relative}:{number}: {match.group(0)}")
    assert found == []


def test_private_range_detector_matches_ranges_and_ignores_versions() -> None:
    private = [
        ".".join(("10", "1", "2", "3")),
        ".".join(("172", "16", "0", "9")),
        ".".join(("172", "31", "255", "254")),
        ".".join(("192", "168", "1", "20")),
        ".".join(("100", "64", "0", "1")),
        ".".join(("100", "127", "255", "255")),
    ]
    for address in private:
        assert PRIVATE_ADDRESS.search(f"http://{address}/api"), address

    for public_or_version in (
        "Pillow>=10.0.0",
        "10.0.0",
        "1.10.0.0",
        ".".join(("172", "15", "0", "1")),
        ".".join(("172", "32", "0", "1")),
        ".".join(("192", "169", "0", "1")),
        ".".join(("100", "63", "0", "1")),
        ".".join(("100", "128", "0", "1")),
        ".".join(("192", "0", "2", "10")),
    ):
        assert not PRIVATE_ADDRESS.search(public_or_version), public_or_version


def test_site_host_detector_matches_names_and_allows_runner_images() -> None:
    for host in (
        "ubuntu" + "-max",
        "windows" + "1",
        "windows" + "2",
        "Windows" + "2",
        "http://" + "ha" + ".local:8123",
        "http://" + "homeassistant" + ".local",
        "http://" + "box" + ".tail1234" + ".ts" + ".net",
    ):
        assert SITE_HOST.search(host), host

    for generic in (
        "ubuntu-latest",
        "ubuntu-latest-arm",
        "ubuntu-latest-extra",
        "ubuntu-24.04",
        "ubuntu-22.04-arm",
        "ha.example.lan",
        "localhost",
        "example.com",
    ):
        assert not SITE_HOST.search(generic), generic
