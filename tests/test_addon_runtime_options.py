"""Exercise the HA launcher with disposable paths and fake process endpoints."""

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash and POSIX process groups")
@pytest.mark.parametrize(
    "configured_min_free, expected_min_free", [(4096, 4096), (0, 0), (None, 2048)]
)
@pytest.mark.parametrize("optional_values", [False, True])
@pytest.mark.parametrize("mutate_options", [False, True])
def test_launcher_passes_all_profiles_and_disk_reserve_to_cli(
    tmp_path: Path,
    configured_min_free: int | None,
    expected_min_free: int,
    optional_values: bool,
    mutate_options: bool,
) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media, fakebin = (tmp_path / name for name in ("data", "media", "bin"))
    for directory in (data, media, fakebin):
        directory.mkdir()
    options = {
        "profiles": [
            "@alice",
            "_bob",
            "https://suno.com/@carol",
            "https://suno.com/playlist/123-abc",
        ],
        "sync_on_start": True,
        "sync_interval_minutes": 15,
        "download_videos": True,
        "covers": False,
        "max_tracks": 12,
        "max_disk_mb": 100,
        "rotate": True,
    }
    if optional_values:
        options["model_version"] = "v6"
        options["jev_url"] = "https://example.test/it's?x=1#part"
        options["session_cookie"] = "token=one'two\\three #still-cookie"
    if configured_min_free is not None:
        options["min_free_mb"] = configured_min_free
    (data / "options.json").write_text(json.dumps(options))
    archive = data / "archive"
    archive.mkdir()
    blocklist = archive / ".blocked"
    blocklist.write_text("existing-blocked-id\n")
    before_blocklist = blocklist.stat()
    capture = tmp_path / "sync.json"
    launcher = tmp_path / "run.sh"
    original = (root / "addon/suno_dl/run.sh").read_text()
    launcher.write_text(original.replace("/data", str(data)).replace("/media/suno", str(media)))
    sleep = fakebin / "sleep"
    sleep.write_text(
        '#!/bin/bash\nif [ "$1" = 3 ]; then exec /bin/sleep 0.01; fi\nexec /bin/sleep 60\n'
    )
    webui = fakebin / "suno-webui"
    webui.write_text(
        '#!/bin/bash\nfor i in {1..500}; do [ -f "$TEST_CAPTURE" ] && exit 0; '
        "/bin/sleep 0.01; done\nexit 1\n"
    )
    fake_cli_python = tmp_path / "fake_cli.py"
    fake_cli_python.write_text(
        "import json, os, sys\n"
        "from suno_dl.config import load_config\n"
        "c = load_config()\n"
        "with open(os.environ['TEST_CAPTURE'], 'w') as f:\n"
        '    keys = [k for k in os.environ if k.startswith("SUNO_")]\n'
        "    data = dict(watch=c.watch, min_free_mb=c.min_free_mb, args=sys.argv[1:])\n"
        '    data["env"] = {k: os.environ[k] for k in keys}\n'
        '    data["snapshot_leaked"] = "TRACKIE_ADDON_OPTIONS_SNAPSHOT" in os.environ\n'
        "    json.dump(data, f)\n"
    )
    cli = fakebin / "suno-dl"
    cli.write_text(
        '#!/bin/bash\nif [ "${1:-}" = "--version" ]; then echo fake; exit 0; fi\n'
        'exec "$TEST_PYTHON" "$TEST_FAKE_CLI" "$@"\n'
    )
    if mutate_options:
        altered = tmp_path / "altered-options.json"
        altered.write_text(json.dumps({"profiles": ["@evil\nSUNO_OUTPUT_DIR=/tmp/bad"]}))
        python_wrapper = fakebin / "python3"
        python_wrapper.write_text(
            "#!/bin/bash\n"
            'if [ ! -e "$TEST_WRAPPER_MARKER" ]; then\n'
            '  "$TEST_PYTHON" "$@"; status=$?\n'
            '  cp "$TEST_ALTERED_OPTIONS" "$TEST_OPTIONS_FILE"\n'
            '  : > "$TEST_WRAPPER_MARKER"\n'
            '  exit "$status"\n'
            "fi\n"
            'exec "$TEST_PYTHON" "$@"\n'
        )
        python_wrapper.chmod(0o755)
    for path in (sleep, webui, cli):
        path.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SUNO_")}
    env.update(
        PATH=f"{fakebin}:{env.get('PATH', '')}",
        PYTHONPATH=str(root / "src"),
        TEST_CAPTURE=str(capture),
        TEST_PYTHON=sys.executable,
        TEST_FAKE_CLI=str(fake_cli_python),
        TEST_WRAPPER_MARKER=str(tmp_path / "snapshot-complete"),
        TEST_ALTERED_OPTIONS=str(tmp_path / "altered-options.json"),
        TEST_OPTIONS_FILE=str(data / "options.json"),
        SUNO_WATCH="@inherited_wrong_profile",
        SUNO_MIN_FREE_MB="99999",
        SUNO_OUTPUT_DIR="/wrong",
        SUNO_DOWNLOAD_VIDEOS="0",
        SUNO_COVERS="1",
        SUNO_MAX_TRACKS="999",
        SUNO_MAX_DISK_MB="999",
        SUNO_ROTATE="0",
        SUNO_JEV_URL="https://inherited.invalid/",
        SUNO_SESSION_COOKIE="inherited-secret",
        SUNO_MODEL_VERSION="old-model",
    )
    with (tmp_path / "launcher.log").open("w") as log:
        process = subprocess.Popen(
            ["bash", str(launcher)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            assert process.wait(timeout=15) == 0, (tmp_path / "launcher.log").read_text()
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
    assert blocklist.read_text() == "existing-blocked-id\n"
    assert blocklist.stat().st_mtime_ns == before_blocklist.st_mtime_ns
    result = json.loads(capture.read_text())
    assert result["snapshot_leaked"] is False
    assert result["watch"] == [
        "@alice",
        "_bob",
        "https://suno.com/@carol",
        "https://suno.com/playlist/123-abc",
    ]
    assert result["min_free_mb"] == expected_min_free
    expected = {
        "SUNO_OUTPUT_DIR": str(media),
        "SUNO_WATCH": ",".join(options["profiles"]),
        "SUNO_DOWNLOAD_VIDEOS": "1",
        "SUNO_COVERS": "0",
        "SUNO_MAX_TRACKS": "12",
        "SUNO_MAX_DISK_MB": "100",
        "SUNO_MIN_FREE_MB": str(expected_min_free),
        "SUNO_ROTATE": "1",
        "SUNO_JEV_URL": options.get("jev_url", ""),
        "SUNO_SESSION_COOKIE": options.get("session_cookie", ""),
        "SUNO_MODEL_VERSION": options.get("model_version", ""),
    }
    assert all(result["env"][key] == value for key, value in expected.items())
    dotenv_file = data / "archive" / ".env"
    assert dotenv_file.stat().st_mode & 0o777 == 0o600
    parsed = dotenv_values(dotenv_file)
    assert all(parsed[key] == value for key, value in expected.items())
    assert "inherited-secret" not in (tmp_path / "launcher.log").read_text()
    assert "inherited-secret" not in dotenv_file.read_text()
    assert "--rotate" in result["args"]
    assert ("--model-version" in result["args"]) == optional_values
    assert "--sync" in result["args"]
    assert result["args"][result["args"].index("--output-dir") + 1] == str(media)


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
@pytest.mark.parametrize(
    "invalid",
    [
        "@alice\nSUNO_OUTPUT_DIR=/tmp/evil",
        "@alice,@bob",
        "@alice # comment",
        "https://suno.com/@alice#fragment",
        "https://suno.com/@alice?token=secret-profile-token",
    ],
)
def test_launcher_rejects_ambiguous_profile_before_writing_dotenv(
    tmp_path: Path, invalid: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media = tmp_path / "data", tmp_path / "media"
    data.mkdir()
    media.mkdir()
    (data / "options.json").write_text(json.dumps({"profiles": [invalid]}))
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "Invalid profile option" in result.stderr
    assert invalid not in result.stdout + result.stderr
    assert "secret-profile-token" not in result.stdout + result.stderr
    assert not (data / "archive" / ".env").exists()


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
@pytest.mark.parametrize("field", ["jev_url", "session_cookie", "model_version"])
@pytest.mark.parametrize("invalid", ["good\nSUNO_ROTATE=1", "${SUNO_OUTPUT_DIR}"])
def test_optional_values_cannot_inject_dotenv(tmp_path: Path, field: str, invalid: str) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media = tmp_path / "data", tmp_path / "media"
    data.mkdir()
    media.mkdir()
    (data / "options.json").write_text(json.dumps({"profiles": ["@safe"], field: invalid}))
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert f"Invalid {field} option" in result.stderr
    assert not (data / "archive" / ".env").exists()
    assert invalid not in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
def test_launcher_refuses_symlinked_dotenv_without_modifying_target(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media = tmp_path / "data", tmp_path / "media"
    archive = data / "archive"
    archive.mkdir(parents=True)
    media.mkdir()
    (data / "options.json").write_text(json.dumps({"profiles": ["@safe"]}))
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    (archive / ".env").symlink_to(target)
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert target.read_text() == "unchanged"
    assert (archive / ".env").is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
@pytest.mark.parametrize("file_type", ["symlink", "hardlink", "fifo"])
def test_launcher_refuses_unsafe_blocklist_without_touching_target(
    tmp_path: Path, file_type: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media = tmp_path / "data", tmp_path / "media"
    archive = data / "archive"
    archive.mkdir(parents=True)
    media.mkdir()
    (data / "options.json").write_text(json.dumps({"profiles": ["@safe"]}))
    target = tmp_path / "external"
    target.write_text("unchanged")
    before = target.stat()
    blocklist = archive / ".blocked"
    if file_type == "symlink":
        blocklist.symlink_to(target)
    elif file_type == "hardlink":
        os.link(target, blocklist)
    else:
        os.mkfifo(blocklist)
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert target.read_text() == "unchanged"
    after = target.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not (archive / ".env").exists()


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
@pytest.mark.parametrize("link_component", ["archive", "parent"])
def test_launcher_refuses_symlinked_archive_path_without_touching_target(
    tmp_path: Path, link_component: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    actual_data = tmp_path / "actual-data"
    actual_data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("unchanged")
    (actual_data / "options.json").write_text(json.dumps({"profiles": ["@safe"]}))
    if link_component == "archive":
        data = actual_data
        (data / "archive").symlink_to(outside, target_is_directory=True)
    else:
        data = tmp_path / "linked-data"
        data.symlink_to(actual_data, target_is_directory=True)
    media = tmp_path / "media"
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert (outside / "sentinel").read_text() == "unchanged"
    assert not (actual_data / "archive" / ".env").exists()
    assert not media.exists()


@pytest.mark.skipif(os.name != "posix", reason="HA launcher uses bash")
@pytest.mark.parametrize(
    "field, value",
    [
        ("sync_on_start", "false"),
        ("audio_only", "false"),
        ("download_videos", "false"),
        ("covers", "false"),
        ("rotate", "false"),
        ("rotate", 0),
        ("sync_interval_minutes", True),
        ("sync_interval_minutes", 14),
        ("sync_interval_minutes", 1441),
        ("max_tracks", -1),
        ("max_tracks", 100001),
        ("max_disk_mb", 1.5),
        ("max_disk_mb", 1000001),
        ("min_free_mb", -1),
        ("min_free_mb", "2048"),
    ],
)
def test_launcher_rejects_invalid_boolean_and_numeric_options(
    tmp_path: Path, field: str, value: object
) -> None:
    root = Path(__file__).resolve().parents[1]
    data, media = tmp_path / "data", tmp_path / "media"
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"profiles": ["@safe"], field: value}))
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        (root / "addon/suno_dl/run.sh")
        .read_text()
        .replace("/data", str(data))
        .replace("/media/suno", str(media))
    )
    result = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert f"Invalid {field} option" in result.stderr
    assert not (data / "archive").exists()
    assert not media.exists()
