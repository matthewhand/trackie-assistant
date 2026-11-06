"""CLI tests: URL validation, exit codes, dry-run, and error surfacing."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from suno_dl.cli import EXIT_NO_TRACKS, EXIT_OK, EXIT_PARTIAL, main


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never let the developer's real .env / cwd leak into CLI tests."""
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("SUNO_DOWNLOAD_VIDEOS", raising=False)
    monkeypatch.delenv("SUNO_COVERS", raising=False)
    monkeypatch.delenv("SUNO_SIDECAR", raising=False)
    monkeypatch.chdir(tmp_path)


def test_version() -> None:
    from suno_dl import __version__

    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    # Derive the expectation from the package so version bumps don't need to
    # touch this test; the important invariant is that the two stay in sync.
    assert __version__ in result.output


def test_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--videos" in result.output
    assert "--no-videos" in result.output
    assert "--audio-only" in result.output
    assert "--covers" in result.output
    assert "--no-covers" in result.output


def test_rotation_help_lists_the_cap_flags() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--max-tracks" in result.output
    assert "--max-disk-mb" in result.output
    assert "--rotate" in result.output
    assert "--no-rotate" in result.output


def test_rotation_flags_reach_run(monkeypatch, tmp_path: Path) -> None:
    """The cap/rotate flags are forwarded to _run (#93)."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(
            max_tracks=kw.get("max_tracks"),
            max_disk_mb=kw.get("max_disk_mb"),
            rotate=kw.get("rotate"),
        )
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--max-tracks",
            "20",
            "--max-disk-mb",
            "512",
            "--rotate",
        ],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert seen == {"max_tracks": 20, "max_disk_mb": 512, "rotate": True}


def test_rotation_defaults_stay_off(monkeypatch, tmp_path: Path) -> None:
    """Without the flags nothing is capped and nothing is deleted (#93)."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(
            max_tracks=kw.get("max_tracks"),
            max_disk_mb=kw.get("max_disk_mb"),
            rotate=kw.get("rotate"),
        )
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.delenv("SUNO_MAX_TRACKS", raising=False)
    monkeypatch.delenv("SUNO_MAX_DISK_MB", raising=False)
    monkeypatch.delenv("SUNO_ROTATE", raising=False)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"max_tracks": 0, "max_disk_mb": 0, "rotate": False}


def test_rotation_caps_from_env(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(max_tracks=kw.get("max_tracks"), rotate=kw.get("rotate"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.setenv("SUNO_MAX_TRACKS", "7")
    monkeypatch.setenv("SUNO_ROTATE", "true")
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"max_tracks": 7, "rotate": True}


def test_sync_forwards_rotation_flags(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(
            max_tracks=kw.get("max_tracks"),
            max_disk_mb=kw.get("max_disk_mb"),
            rotate=kw.get("rotate"),
        )
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        ["--sync", "--watch", "@drinko", "--max-tracks", "3", "--rotate"],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert seen == {"max_tracks": 3, "max_disk_mb": 0, "rotate": True}


def test_summary_reports_rotation_counts(monkeypatch, tmp_path: Path) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 6,
            "downloaded": 1,
            "skipped_exists": 2,
            "failed": 0,
            "skipped_video": 0,
            "skipped_capped": 2,
            "skipped_rotated": 1,
            "rotated_tracks": 3,
            "rotated_files": 4,
            "rotated_bytes": 5 * 1024 * 1024,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert "capped=2" in result.output
    assert "pruned_skipped=1" in result.output
    assert "rotated=3" in result.output
    assert "rotated_files=4" in result.output
    assert "rotated_mb=5.0" in result.output


def test_rejects_non_suno_url() -> None:
    result = CliRunner().invoke(main, ["--url", "https://example.com/@x"])
    assert result.exit_code != 0
    assert "Invalid Suno URL" in result.output


def test_rejects_bad_scheme() -> None:
    result = CliRunner().invoke(main, ["--url", "http://suno.com/@x"])
    assert result.exit_code != 0


def test_no_tracks_exit_code(monkeypatch) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 0,
            "downloaded": 0,
            "skipped_exists": 0,
            "failed": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    result = CliRunner().invoke(main, ["--url", "https://suno.com/@drinko"])
    assert result.exit_code == EXIT_NO_TRACKS


def test_audio_only_and_covers_flags_reach_run(monkeypatch, tmp_path: Path) -> None:
    """The opt-in disk-saver flags must be forwarded to _run."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(audio_only=kw.get("audio_only"), covers=kw.get("covers"))
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--audio-only",
            "--covers",
        ],
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"audio_only": True, "covers": True}


def test_defaults_skip_videos_and_fetch_covers(monkeypatch, tmp_path: Path) -> None:
    """Default: audio_only (no videos) + covers on (#85)."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(audio_only=kw.get("audio_only"), covers=kw.get("covers"))
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.delenv("SUNO_DOWNLOAD_VIDEOS", raising=False)
    monkeypatch.delenv("SUNO_COVERS", raising=False)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"audio_only": True, "covers": True}


def test_videos_flag_disables_audio_only(monkeypatch, tmp_path: Path) -> None:
    """--videos opts into MP4 downloads (audio_only=False)."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(audio_only=kw.get("audio_only"), covers=kw.get("covers"))
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--videos",
            "--no-covers",
        ],
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"audio_only": False, "covers": False}


def test_summary_includes_video_skipped(monkeypatch, tmp_path: Path) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 4,
            "downloaded": 2,
            "skipped_exists": 1,
            "failed": 0,
            "skipped_video": 1,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert "video_skipped=1" in result.output


def test_partial_failure_exit_code(monkeypatch, tmp_path: Path) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 3,
            "downloaded": 2,
            "skipped_exists": 0,
            "failed": 1,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_PARTIAL


def test_success_exit_code_and_destination(monkeypatch, tmp_path: Path) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 2,
            "downloaded": 2,
            "skipped_exists": 0,
            "failed": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert "Saved to" in result.output


def test_rate_limit_reaches_run(monkeypatch, tmp_path: Path) -> None:
    """--rate-limit is forwarded to _run; absent flag forwards None."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(rate_limit=kw.get("rate_limit"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--rate-limit",
            "0.25",
        ],
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"rate_limit": 0.25}

    seen.clear()
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"rate_limit": None}


def test_min_free_mb_reaches_run(monkeypatch, tmp_path: Path) -> None:
    """--min-free-mb is forwarded to _run; absent flag uses config default."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(min_free_mb=kw.get("min_free_mb"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--min-free-mb",
            "512",
        ],
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"min_free_mb": 512}

    seen.clear()
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    # When not provided via CLI, the config default (2048) is used
    assert seen == {"min_free_mb": 2048}


def test_flat_and_extract_audio_plumbing(monkeypatch) -> None:
    kwargs_seen = {}

    async def fake_run(*a, **kw):
        kwargs_seen.update(kw)
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--flat", "--extract-audio"]
    )
    assert result.exit_code == 0, result.output
    assert kwargs_seen["flat"] is True
    assert kwargs_seen["extract_audio"] is True


@pytest.mark.parametrize(
    "flags,valid",
    [
        (["--videos", "--extract-audio", "--keep-video"], True),
        (["--no-videos", "--extract-audio", "--keep-video"], False),
        (["--videos", "--no-extract-audio", "--keep-video"], False),
        (["--no-videos", "--extract-audio"], True),
        (["--videos", "--extract-audio"], True),
    ],
)
def test_keep_video_requires_explicit_webui_opt_in(monkeypatch, flags, valid) -> None:
    """CLI rejects invalid modes before any real network or media operations."""
    seen: list[bool] = []

    async def fake_run(*args, **kwargs):
        seen.append(kwargs["keep_video"])
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        ["--url", "https://suno.com/@drinko", *flags],
    )
    if valid:
        assert result.exit_code == 0, result.output
        assert seen == ["--keep-video" in flags]
    else:
        assert result.exit_code != 0
        assert "--keep-video requires --videos and --extract-audio" in result.output
        assert seen == []


def test_default_is_subdir_not_flat(monkeypatch) -> None:
    kwargs_seen = {}

    async def fake_run(*a, **kw):
        kwargs_seen.update(kw)
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    result = CliRunner().invoke(main, ["--url", "https://suno.com/@drinko"])
    assert result.exit_code == 0, result.output
    assert kwargs_seen["flat"] is False


# --- one-shot retag of an archive that predates tags ---------------


def _m4a(dest: Path) -> Path:
    dest.write_bytes((Path(__file__).parent / "fixtures" / "tiny.m4a").read_bytes())
    return dest


def test_retag_help_lists_the_migration_flags() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--retag" in result.output
    assert "--artist" in result.output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_retag_tags_an_existing_archive(tmp_path: Path) -> None:
    from suno_dl.tagging import read_tags

    pytest.importorskip("mutagen")
    track = _m4a(tmp_path / "drinko_Ajani, Nacatl Pariah (feat. Pyotr Kropotkin).m4a")
    result = CliRunner().invoke(
        main, ["--retag", str(tmp_path), "--retag-archive-root", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "Retag summary:" in result.output
    assert "tagged=1" in result.output
    tags = read_tags(track)
    assert tags["artist"] == "drinko"
    assert tags["title"] == "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_retag_makes_no_network_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The migration is local-only: no fetcher is ever constructed."""
    import suno_dl.cli as cli_mod

    def explode(*a, **kw):  # pragma: no cover — must never run
        raise AssertionError("--retag must not touch the network")

    _m4a(tmp_path / "drinko_Title.m4a")
    monkeypatch.setattr(cli_mod, "_make_fetcher", explode)
    result = CliRunner().invoke(
        main, ["--retag", str(tmp_path), "--retag-archive-root", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK, result.output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_retag_is_idempotent(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    _m4a(tmp_path / "drinko_Title.m4a")
    runner = CliRunner()
    assert (
        runner.invoke(
            main, ["--retag", str(tmp_path), "--retag-archive-root", str(tmp_path)]
        ).exit_code
        == EXIT_OK
    )
    second = runner.invoke(main, ["--retag", str(tmp_path), "--retag-archive-root", str(tmp_path)])
    assert second.exit_code == EXIT_OK
    assert "tagged=0" in second.output
    assert "already_tagged=1" in second.output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_retag_dry_run_writes_nothing(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    from suno_dl.tagging import read_tags

    track = _m4a(tmp_path / "drinko_Title.m4a")
    before = track.read_bytes()
    result = CliRunner().invoke(main, ["--retag", str(tmp_path), "--dry-run"])
    assert result.exit_code == EXIT_OK, result.output
    assert "tagged=1" in result.output
    assert track.read_bytes() == before
    assert read_tags(track) == {}


def test_retag_rejects_a_missing_directory(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--retag", str(tmp_path / "nope")])
    assert result.exit_code != 0
    assert "not a directory" in result.output


def test_artist_without_retag_is_rejected(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--url", "https://suno.com/@drinko", "--artist", "drinko"])
    assert result.exit_code != 0
    assert "--artist is only used with --retag" in result.output


def test_summary_reports_tagged_count(monkeypatch, tmp_path: Path) -> None:
    async def fake_run(*a, **kw):
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
            "tagged": 1,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert "tagged=1" in result.output


def test_no_sidecar_flag_reaches_run(monkeypatch, tmp_path: Path) -> None:
    """Sidecars are on by default; --no-sidecar and SUNO_SIDECAR=0 turn them off."""
    seen: list[object] = []

    async def fake_run(*a, **kw):
        seen.append(kw.get("sidecar"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    base = ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    assert CliRunner().invoke(main, base).exit_code == EXIT_OK
    assert CliRunner().invoke(main, [*base, "--no-sidecar"]).exit_code == EXIT_OK
    monkeypatch.setenv("SUNO_SIDECAR", "0")
    assert CliRunner().invoke(main, base).exit_code == EXIT_OK
    assert CliRunner().invoke(main, [*base, "--sidecar"]).exit_code == EXIT_OK
    assert seen == [True, False, False, True]


# --- model-version filter -------------------------------------------------------


def test_model_version_option_appears_in_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--model-version" in result.output


def test_model_version_reaches_run(monkeypatch, tmp_path: Path) -> None:
    """--model-version is forwarded to _run."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(model_version=kw.get("model_version"))
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/@drinko",
            "--output-dir",
            str(tmp_path),
            "--model-version",
            "v6",
        ],
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"model_version": "v6"}


def test_model_version_reads_environment_for_dashboard(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    async def fake_run(*args, **kwargs):
        seen.update(model_version=kwargs.get("model_version"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setenv("SUNO_MODEL_VERSION", "v6")
    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["model_version"] == "v6"


def test_model_version_reads_archive_dotenv_before_click_parsing(
    monkeypatch, tmp_path: Path
) -> None:
    seen = {}

    async def fake_run(*args, **kwargs):
        seen.update(model_version=kwargs.get("model_version"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setenv("SUNO_MODEL_VERSION", "")
    monkeypatch.delenv("SUNO_MODEL_VERSION", raising=False)
    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    (tmp_path / ".env").write_text("SUNO_MODEL_VERSION=v6\n")
    result = CliRunner().invoke(main, ["--url", "https://suno.com/@drinko"])
    assert result.exit_code == 0, result.output
    assert seen["model_version"] == "v6"


def test_model_version_defaults_to_none(monkeypatch, tmp_path: Path) -> None:
    """Without --model-version, _run receives None (no filter)."""
    seen: dict[str, object] = {}

    async def fake_run(*a, **kw):
        seen.update(model_version=kw.get("model_version"))
        return {
            "total": 1,
            "downloaded": 1,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_OK
    assert seen == {"model_version": None}


def test_mutating_retag_requires_declared_archive_root(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--retag", str(tmp_path)])
    assert result.exit_code != 0
    assert "--retag-archive-root" in result.output
    assert list(tmp_path.iterdir()) == []


def test_retag_rejects_target_outside_declared_archive(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    result = CliRunner().invoke(main, ["--retag", str(target), "--retag-archive-root", str(root)])
    assert result.exit_code != 0
    assert "inside the declared archive root" in result.output
    assert list(root.iterdir()) == []
    assert list(target.iterdir()) == []


def test_sync_output_and_summary_do_not_disclose_source_queries(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SUNO_WATCH", "")
    from suno_dl.errors import DownloadError

    seen = []

    async def fake_run(url, *args, **kwargs):
        seen.append(url)
        if "playlist" in url:
            raise DownloadError(f"request {url} failed with token=EXCEPTION_SECRET")
        return {"total": 1, "downloaded": 1, "failed": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    first = "https://suno.com/playlist/abc?token=SOURCE_SECRET"
    second = "https://suno.com/song/def?token=SECOND_SECRET#FRAGMENT_SECRET"
    result = CliRunner().invoke(
        main, ["--sync", "--watch", first, "--watch", second, "--output-dir", str(tmp_path)]
    )
    assert result.exit_code == EXIT_PARTIAL
    assert seen == [first, second]
    assert "Sync summary:" in result.output
    assert "download failed" in result.output
    for secret in ("SOURCE_SECRET", "SECOND_SECRET", "FRAGMENT_SECRET", "EXCEPTION_SECRET"):
        assert secret not in result.output


def test_sync_invalid_watch_url_does_not_disclose_credentials(monkeypatch, tmp_path: Path):
    value = "https://wrong.example/@name?token=SOURCE_SECRET"
    result = CliRunner().invoke(main, ["--sync", "--watch", value, "--output-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "Invalid watch-list source" in result.output
    assert "SOURCE_SECRET" not in result.output


@pytest.mark.parametrize("error_type", ["fetch", "unexpected", "tag", "migration", "intent"])
def test_direct_source_failure_redacts_url_and_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: str
) -> None:
    from suno_dl.download_intent import UnsafeDownloadIntent
    from suno_dl.errors import FetchError
    from suno_dl.producer_migration import MigrationError
    from suno_dl.tagging import TagPublicationUncertain

    source = "https://suno.com/@drinko?token=SOURCE_SECRET#FRAGMENT_SECRET"
    seen = []

    async def fail(url, *args, **kwargs):
        seen.append(url)
        message = f"request {url}; https://cdn.example/audio?signature=CDN_SECRET"
        errors = {
            "fetch": FetchError,
            "unexpected": OSError,
            "tag": TagPublicationUncertain,
            "migration": MigrationError,
            "intent": UnsafeDownloadIntent,
        }
        raise errors[error_type](message)

    monkeypatch.setattr("suno_dl.cli._run", fail)
    result = CliRunner().invoke(main, ["--url", source, "--output-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert seen == [source]
    assert "?...redacted" in result.output
    expected = {
        "fetch": "could not fetch the source",
        "unexpected": "unexpected OSError",
        "tag": "tag replacement may have occurred; stop and inspect",
        "migration": "migration safety check failed; stop and inspect",
        "intent": "archive write safety could not be established; stop and inspect",
    }
    assert expected[error_type] in result.output
    for secret in ("SOURCE_SECRET", "FRAGMENT_SECRET", "CDN_SECRET"):
        assert secret not in result.output


@pytest.mark.parametrize("control", ["\n", "\r", "\x1b", "\x85", "\u2028", "\u202e"])
def test_sync_rejects_control_characters_in_bare_handle_without_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: str
) -> None:
    source = "@alice" + control + "FORGED_MESSAGE"

    async def unexpected(*args, **kwargs):
        pytest.fail("invalid source reached sync")

    monkeypatch.setattr("suno_dl.cli._run", unexpected)
    result = CliRunner().invoke(main, ["--sync", "--watch", source, "--output-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "Invalid watch-list source" in result.output
    assert "FORGED_MESSAGE" not in result.output


def test_direct_url_rejects_control_character_without_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid source reached discovery")

    monkeypatch.setattr("suno_dl.cli._run", unexpected)
    value = "https://suno.com/@alice\nFORGED_MESSAGE"
    result = CliRunner().invoke(main, ["--url", value, "--output-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "Invalid Suno URL" in result.output
    assert "FORGED_MESSAGE" not in result.output


@pytest.mark.parametrize(
    "detail", ["Windows archive writes are unavailable", "interrupted publication requires review"]
)
def test_direct_unsafe_write_error_shows_platform_neutral_safety_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detail: str
) -> None:
    from suno_dl.download_intent import UnsafeDownloadIntent

    async def fail(*args, **kwargs):
        raise UnsafeDownloadIntent(detail + " secret=DO_NOT_PRINT")

    monkeypatch.setattr("suno_dl.cli._run", fail)
    result = CliRunner().invoke(
        main, ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "archive write safety could not be established" in result.output
    assert "platform support" in result.output
    assert "journal status" in result.output
    assert "DO_NOT_PRINT" not in result.output
    assert "pending intent" not in result.output
