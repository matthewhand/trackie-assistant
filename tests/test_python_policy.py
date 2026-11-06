"""Python 3.13-only support policy contract (#121).

The manual CI harness and the hosted workflows must never disagree with the
package metadata again: a `requires-python` bump without the matrix (or the
other way round) silently claims support for an interpreter nothing tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = "3.13"
RETIRED = ("3.10", "3.11", "3.12")


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text()


def test_package_metadata_supports_only_python_313() -> None:
    project = _read("pyproject.toml")
    assert re.search(r'^requires-python = ">=3\.13"$', project, re.M)
    for version in RETIRED:
        assert f'"Programming Language :: Python :: {version}"' not in project
    assert '"Programming Language :: Python :: 3.13"' in project
    assert re.search(r'^target-version = "py313"$', project, re.M)
    assert re.search(r'^python_version = "3\.13"$', project, re.M)


@pytest.mark.parametrize("workflow", [".github/workflows/ci.yml", ".github/workflows/release.yml"])
def test_workflows_matrix_is_python_313_only(workflow: str) -> None:
    content = _read(workflow)
    assert f'python-version: ["{SUPPORTED}"]' in content
    versions = re.findall(r'python-version: "([0-9.]+)"', content)
    assert versions, content
    assert set(versions) == {SUPPORTED}, versions
    for version in RETIRED:
        assert version not in content, f"{workflow} still references Python {version}"


def test_local_ci_harness_is_python_313_only() -> None:
    script = _read("scripts", "ci-local.sh")
    assert 'PYTHONS="${CI_PYTHONS:-3.13}"' in script
    assert 'BUILD_PY="${CI_BUILD_PYTHON:-3.13}"' in script
    assert "3.10" not in script and "3.11" not in script and "3.12" not in script
    unit = _read("scripts", "suno-ci-local.service")
    assert 'Environment="CI_PYTHONS=3.13"' in unit


def test_container_and_docs_state_the_same_minimum() -> None:
    assert "python-3.13" in _read("addon", "suno_dl", "Dockerfile")
    # Enforce the documented minimum without requiring a particular badge or
    # Markdown emphasis style on the landing page.
    assert re.search(rf"\bPython\s+(?:\*\*)?{re.escape(SUPPORTED)}\+", _read("README.md"))
    assert "Python 3.13 on Linux." in _read("CONTRIBUTING.md")
    assert "pytest 3.13" in _read("docs", "diagrams", "specs", "10-operations.json")
    for path in ("README.md", "CONTRIBUTING.md", "docs/handoff.md", "docs/release-verification.md"):
        text = _read(path)
        for version in RETIRED:
            assert version not in text, f"{path} still advertises Python {version}"
