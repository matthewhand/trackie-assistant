"""Synthetic release artifact contract: no installation or network needed."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.version import Version

from suno_dl import __version__

ROOT = Path(__file__).resolve().parents[1]
WHEEL_DIR = ROOT / "addon/suno_dl"
APPROVED_HERO_SHA256 = "a267903a5aba7a219c1ba2711395080cb1f0fd3c7444d0b759566236ae8f3ef1"
STATIC = ROOT / "src/suno_dl/webui_static"


def _require_wheel_member(archive: zipfile.ZipFile, suffix: str) -> str:
    """Return a required ZIP member or fail with a diagnostic assertion."""
    name = next((n for n in archive.namelist() if n.endswith(suffix)), None)
    assert name is not None, f"Missing required wheel member: {suffix}"
    return name


def test_release_candidate_version_and_addon_identity() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^name = "trackie-assistant"$', project, re.M)
    assert re.search(r'^version = "' + re.escape(__version__) + r'"$', project, re.M)
    assert 'requires-python = ">=3.13"' in project
    assert 'suno-dl = "suno_dl.cli:main"' in project
    assert 'suno-webui = "suno_dl.webui:webui_cli"' in project
    assert 'suno-recommend = "suno_dl.recommend:recommend_cli"' in project

    addon = json.loads((ROOT / "addon/suno_dl/config.json").read_text())
    assert addon["name"] == addon["panel_title"] == "Trackie Assistant"
    assert Version(addon["version"]) == Version(__version__)
    assert addon["arch"] == ["amd64"]
    repository = json.loads((ROOT / "repository.json").read_text())
    assert repository["url"] == addon["url"]
    assert repository["name"].startswith("Trackie Assistant")
    assert addon["slug"] == "suno_dl"
    assert addon["ingress"] is True and addon["ingress_port"] == 8760
    assert addon["webui"] == "http://[HOST]:[PORT:8760]"
    assert addon["map"] == ["media:rw", "share:rw"]
    assert addon["homeassistant_api"] is False
    assert "download_videos" in addon["options"] and addon["options"]["download_videos"] is False
    assert addon["options"]["rotate"] is False
    assert addon["options"]["profiles"] == []
    assert addon["options"]["sync_on_start"] is False

    docs = (ROOT / "addon/suno_dl/DOCS.md").read_text()
    assert docs.startswith("# Trackie Assistant\n")
    assert "**1.6.1**" in docs and "**1.5.3**" in docs
    assert "local_suno_dl" in docs
    assert "ha apps rebuild local_suno_dl" in docs


def test_approved_original_hero_and_release_readme() -> None:
    source = (ROOT / "docs/trackie-assistant-logo.webp").read_bytes()
    assert hashlib.sha256(source).hexdigest() == APPROVED_HERO_SHA256
    assert (STATIC / "trackie-assistant-logo.webp").read_bytes() == source
    text = (ROOT / "README.md").read_text()
    assert text.startswith("# Trackie Assistant\n\n![")
    assert "(docs/trackie-assistant-banner.png)" in text
    assert (
        (ROOT / "docs/trackie-assistant-banner.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    )
    assert "## Compatibility with existing installations" in text


def test_vendored_wheel_matches_source_without_installation() -> None:
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    assert [f.name for f in wheels] == [f"trackie_assistant-{__version__}-py3-none-any.whl"]

    with zipfile.ZipFile(wheels[0]) as archive:
        assert archive.testzip() is None
        metadata_name = _require_wheel_member(archive, ".dist-info/METADATA")
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        assert metadata["Name"] == "trackie-assistant"
        assert metadata["Version"] == __version__
        assert "Trackie Assistant" in metadata["Summary"]
        assert metadata["Requires-Python"] == ">=3.13"

        entrypoints_name = _require_wheel_member(archive, ".dist-info/entry_points.txt")
        entrypoints = archive.read(entrypoints_name).decode()
        for expected in (
            "suno-dl = suno_dl.cli:main",
            "suno-webui = suno_dl.webui:webui_cli",
            "suno-recommend = suno_dl.recommend:recommend_cli",
        ):
            assert expected in entrypoints

        # Compare the whole package, not a sample: the vendored wheel drifted
        # for cli.py/discovery.py while this test only spot-checked two files.
        source = ROOT / "src/suno_dl"
        expected = {
            f"suno_dl/{path.relative_to(source).as_posix()}": path
            for path in source.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and (path.suffix in {".py", ".js", ".svg", ".webp", ".json"} or path.name == "py.typed")
        }
        shipped = {name for name in archive.namelist() if name.startswith("suno_dl/")}
        assert shipped == set(expected), shipped.symmetric_difference(expected)
        for member, path in sorted(expected.items()):
            assert archive.read(member) == path.read_bytes(), f"stale wheel member: {member}"
        hero = archive.read("suno_dl/webui_static/trackie-assistant-logo.webp")
        assert hashlib.sha256(hero).hexdigest() == APPROVED_HERO_SHA256
        assert b"<title>Trackie Assistant</title>" in archive.read("suno_dl/webui.py")


def test_changelog_and_addon_build_contract() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert changelog.startswith("# Changelog")
    assert "Release candidate alignment (not yet tagged or deployed)" in changelog
    assert "latest published" in changelog.lower()
    assert "1.5.3" in changelog and "1.6.1" in changelog
    dockerfile = (ROOT / "addon/suno_dl/Dockerfile").read_text()
    assert "COPY trackie_assistant-*.whl /tmp/" in dockerfile
    assert "pip install --no-cache-dir /tmp/trackie_assistant-*.whl" in dockerfile
    run = (ROOT / "addon/suno_dl/run.sh").read_text()
    assert "suno-webui --host 0.0.0.0 --port 8760" in run
    import json

    addon = json.loads((ROOT / "addon/suno_dl/config.json").read_text())
    assert addon["ports"]["8760/tcp"] is None  # ingress only; no exposed host port
    assert "/media/suno" in run


def _metadata_body(raw: bytes) -> str:
    """Decode the long description explicitly.

    ``BytesParser().get_payload()`` decodes non-MIME payloads as ASCII with
    replacement characters, which made an intact UTF-8 README look corrupted
    (149 bogus U+FFFD) during a manual audit. Decode it as the UTF-8 that
    setuptools actually wrote.
    """
    _, _, body = raw.partition(b"\n\n")
    return body.decode("utf-8")


def test_vendored_wheel_matches_reviewed_release_bytes() -> None:
    """Published packages are immutable; main's landing page can evolve.

    Pin the reviewed rc1 archive rather than forcing an artifact rebuild for
    prose changes. Source-member checks above still detect application drift.
    The pinned hash must change only with a separately verified new release.
    """
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    assert len(wheels) == 1
    reviewed = {
        "1.6.2rc16": "6546c1db250045f3c718b639b3ee6e9c2d1f944d7dbb0cca1de428eb3892088d",
        "1.6.2rc15": "6c102a4ce73cafebd8066e9cf5c18f815feb24da6d254151da8c1c9bb181d48c",
        "1.6.2rc14": "936bf95c783d8cb73ad62653a0c64c4d64bca81398ec9960928afcac503b5408",
        "1.6.2rc13": "9b342a4cb1b03e1ee34ae66a79f029dc3e0fcbaeeff93d2ce0b06047f1d1b1e3",
        "1.6.2rc12": "629ea79d2539411f3f3e82ae981128cbb7221c84153b3b311501874bc14a44c0",
        "1.6.2rc11": "9660537f201dc504d75ba582f6f6e108098df65fab0f2a775a6dc3b92081bc2d",
        "1.6.2rc10": "f5ba0a5f890d96955fc28ae952b4f0bf49d7811c6b7bb16d249e783042252f7f",
        "1.6.2rc9": "b58a952748d579902b30355de94605a26293543d74cd2f4ba2f7a15766842b24",
        "1.6.2rc8": "25dab6b97c5f47f4ddc85958b80578f413dac3727b183df9de61e9cce44d0bcd",
        "1.6.2rc7": "3b23727fcf0e5d9e22b6ff78f77aa71be4d541a81138d9d7f5983c309c580fa8",
        "1.6.2rc6": "817a79cf45674690a13ac6aa0ac023367220ddd70d45a4f809a8e8dccb223870",
        "1.6.2rc5": "5f887451d8913c334f68b7b3a5da5dd4e85c420c86f2c02aa3f85c17ab6d0843",
        "1.6.2rc4": "62c57c6627b5875a16dd9d93ed2389b2724a054278b5ada882ea2e97c70bb4f0",
        "1.6.2rc3": "05218542c979a9b1b36348a951eff0a15d2d5cb7aa3326f69438c55ae9dc73d4",
        "1.6.2rc1": "e2ba4803f6635f16e718f522a07cd7724fadcfb7d1c4ba4e14c32d83e6467c25",
        "1.6.2rc2": "71c4802a1a54e7cc4304881277cd832c279170e26e4e6ad7d8674a37655bd8c8",
    }
    assert hashlib.sha256(wheels[0].read_bytes()).hexdigest() == reviewed[__version__]


def test_wheel_metadata_has_no_replacement_characters() -> None:
    """Guard the encoding path itself: mojibake here reaches every index page."""
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_name = _require_wheel_member(archive, ".dist-info/METADATA")
        body = _metadata_body(archive.read(metadata_name))

    assert "\ufffd" not in body
    assert body.strip() == (ROOT / "README.md").read_text(encoding="utf-8").strip()
