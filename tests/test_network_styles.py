"""Unit tests for network style analysis."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from suno_dl.models import TrackInfo
from suno_dl.network_styles import (
    NetworkProfile,
    analyze_network_styles,
    catalogue_genre_groups,
    catalogue_style_counter,
    dump_csv,
    dump_json,
    load_catalogue,
    print_summary,
)

FIXTURES = Path(__file__).parent / "fixtures"
CATALOGUE_PATH = FIXTURES / "genres.json"


@pytest.fixture
def catalogue() -> dict:
    return load_catalogue(CATALOGUE_PATH)


@pytest.fixture
def sample_profiles() -> list[NetworkProfile]:
    return [
        NetworkProfile(
            handle="alice",
            url="https://suno.com/@alice",
            tracks=[
                TrackInfo(
                    id="a1b2c3d4-0001-4000-8000-000000000001",
                    title="Neon Nights",
                    page_url="https://suno.com/song/a1b2c3d4-0001-4000-8000-000000000001",
                    style_tags=["synthwave", "electropop"],
                    play_count=1500,
                ),
                TrackInfo(
                    id="a1b2c3d4-0002-4000-8000-000000000002",
                    title="Bass Drop",
                    page_url="https://suno.com/song/a1b2c3d4-0002-4000-8000-000000000002",
                    style_tags=["dubstep", "edm"],
                    play_count=2300,
                ),
            ],
            followers=1200,
            following=340,
        ),
        NetworkProfile(
            handle="bob",
            url="https://suno.com/@bob",
            tracks=[
                TrackInfo(
                    id="a1b2c3d4-0003-4000-8000-000000000003",
                    title="Hard Trap",
                    page_url="https://suno.com/song/a1b2c3d4-0003-4000-8000-000000000003",
                    style_tags=["trap", "drill"],
                    play_count=5600,
                ),
                TrackInfo(
                    id="a1b2c3d4-0004-4000-8000-000000000004",
                    title="Lo-Fi Study",
                    page_url="https://suno.com/song/a1b2c3d4-0004-4000-8000-000000000004",
                    style_tags=["lo-fi hip hop", "chillhop"],
                    play_count=8900,
                ),
            ],
            followers=800,
            following=120,
        ),
        NetworkProfile(
            handle="carol",
            url="https://suno.com/@carol",
            tracks=[
                TrackInfo(
                    id="a1b2c3d4-0005-4000-8000-000000000005",
                    title="Jazz Cafe",
                    page_url="https://suno.com/song/a1b2c3d4-0005-4000-8000-000000000005",
                    style_tags=["bebop", "cool jazz"],
                    play_count=450,
                ),
            ],
            followers=300,
            following=50,
        ),
    ]


class TestLoadCatalogue:
    def test_loads_fixture(self, catalogue: dict) -> None:
        assert "genres" in catalogue
        assert "songs" in catalogue
        assert len(catalogue["songs"]) == 247
        assert len(catalogue["genres"]) == 10

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_catalogue(tmp_path / "nonexistent.json")
        assert result == {"genres": {}, "songs": []}

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        custom = tmp_path / "custom.json"
        custom.write_text(json.dumps({"genres": {"test": ["style"]}, "songs": []}))
        monkeypatch.setenv("SUNO_CATALOGUE_PATH", str(custom))
        result = load_catalogue()
        assert result["genres"] == {"test": ["style"]}


class TestCatalogueStyleCounter:
    def test_counts_styles(self, catalogue: dict) -> None:
        counter = catalogue_style_counter(catalogue)
        assert counter["synthwave"] >= 3
        assert counter["trap"] >= 3
        assert counter["bebop"] >= 2

    def test_empty_catalogue(self) -> None:
        counter = catalogue_style_counter({"songs": []})
        assert len(counter) == 0

    def test_case_insensitive(self) -> None:
        catalogue = {"songs": [{"styles": ["Synthwave", "SYNTHWAVE", "synthwave"]}]}
        counter = catalogue_style_counter(catalogue)
        assert counter["synthwave"] == 3


class TestCatalogueGenreGroups:
    def test_extracts_genres(self, catalogue: dict) -> None:
        groups = catalogue_genre_groups(catalogue)
        assert "electronic" in groups
        assert "hip-hop" in groups
        assert "synthwave" in groups["electronic"]

    def test_empty_catalogue(self) -> None:
        groups = catalogue_genre_groups({})
        assert groups == {}


class TestAnalyzeNetworkStyles:
    def test_basic_analysis(self, sample_profiles: list[NetworkProfile], catalogue: dict) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        assert result.seed_handle == "alice"
        assert len(result.network_profiles) == 3
        assert result.aggregate_styles["synthwave"] == 1
        assert result.aggregate_styles["trap"] == 1
        assert result.aggregate_styles["bebop"] == 1

    def test_absent_styles(self, sample_profiles: list[NetworkProfile], catalogue: dict) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        assert "k-pop" in result.absent_styles
        assert "flamenco" in result.absent_styles
        assert "gamelan" in result.absent_styles

    def test_rare_styles(self, sample_profiles: list[NetworkProfile], catalogue: dict) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        rare_style_names = [s for s, _ in result.rare_styles]
        assert "dubstep" in rare_style_names
        assert "drill" in rare_style_names

    def test_style_rankings(self, sample_profiles: list[NetworkProfile], catalogue: dict) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        assert len(result.style_rankings) > 0
        top = result.style_rankings[0]
        assert "style" in top
        assert "network_count" in top
        assert "catalogue_count" in top
        assert "ratio" in top
        assert "absent" in top
        assert "rare" in top

    def test_empty_network(self, catalogue: dict) -> None:
        result = analyze_network_styles([], catalogue)
        assert result.seed_handle == ""
        assert len(result.aggregate_styles) == 0
        assert len(result.absent_styles) == len(catalogue_style_counter(catalogue))

    def test_case_insensitive_aggregation(self, catalogue: dict) -> None:
        profiles = [
            NetworkProfile(
                handle="test",
                url="https://suno.com/@test",
                tracks=[
                    TrackInfo(
                        id="a1b2c3d4-0006-4000-8000-000000000006",
                        title="Test",
                        page_url="https://suno.com/song/test",
                        style_tags=["Synthwave", "SYNTHWAVE", "synthwave"],
                    )
                ],
            )
        ]
        result = analyze_network_styles(profiles, catalogue)
        assert result.aggregate_styles["synthwave"] == 3

    def test_no_style_tags(self, catalogue: dict) -> None:
        profiles = [
            NetworkProfile(
                handle="test",
                url="https://suno.com/@test",
                tracks=[
                    TrackInfo(
                        id="a1b2c3d4-0007-4000-8000-000000000007",
                        title="Test",
                        page_url="https://suno.com/song/test",
                    )
                ],
            )
        ]
        result = analyze_network_styles(profiles, catalogue)
        assert len(result.aggregate_styles) == 0


class TestDumpJson:
    def test_writes_valid_json(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, tmp_path: Path
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        output = tmp_path / "output.json"
        dump_json(result, output)
        data = json.loads(output.read_text())
        assert data["seed_handle"] == "alice"
        assert len(data["network_profiles"]) == 3
        assert "aggregate_styles" in data
        assert "catalogue_styles" in data
        assert "absent_styles" in data
        assert "rare_styles" in data
        assert "style_rankings" in data

    def test_track_data_in_json(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, tmp_path: Path
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        output = tmp_path / "output.json"
        dump_json(result, output)
        data = json.loads(output.read_text())
        first_track = data["network_profiles"][0]["tracks"][0]
        assert first_track["styles"] == ["synthwave", "electropop"]
        assert first_track["play_count"] == 1500


class TestDumpCsv:
    def test_writes_valid_csv(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, tmp_path: Path
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        output = tmp_path / "output.csv"
        dump_csv(result, output)
        lines = output.read_text().strip().split("\n")
        assert lines[0] == "style,network_count,catalogue_count,ratio,absent,rare"
        assert len(lines) > 1

    def test_csv_contains_expected_styles(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, tmp_path: Path
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        output = tmp_path / "output.csv"
        dump_csv(result, output)
        content = output.read_text()
        assert "synthwave" in content
        assert "trap" in content
        assert "bebop" in content


class TestPrintSummary:
    def test_prints_summary(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, capsys: pytest.CaptureFixture
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        print_summary(result)
        captured = capsys.readouterr()
        assert "Network Style Analysis for @alice" in captured.out
        assert "Profiles analyzed: 3" in captured.out
        assert "Unique style tags in network" in captured.out
        assert "Absent styles" in captured.out
        assert "Rare styles" in captured.out
        assert "Top styles by network frequency" in captured.out

    def test_prints_top_styles(
        self, sample_profiles: list[NetworkProfile], catalogue: dict, capsys: pytest.CaptureFixture
    ) -> None:
        result = analyze_network_styles(sample_profiles, catalogue)
        print_summary(result)
        captured = capsys.readouterr()
        assert "synthwave" in captured.out
        assert "trap" in captured.out


class TestNetworkProfileDataclass:
    def test_default_tracks(self) -> None:
        profile = NetworkProfile(handle="test", url="https://suno.com/@test")
        assert profile.tracks == []
        assert profile.followers is None
        assert profile.following is None


class TestStyleStatsDataclass:
    def test_default_play_counts(self) -> None:
        from suno_dl.network_styles import StyleStats

        stats = StyleStats(handle="test", track_count=0, style_counts=Counter())
        assert stats.play_counts == {}


class TestShippedCatalogue:
    """The default catalogue must ship inside the package (#packaging).

    The previous default (``tests/fixtures/genres.json``) only resolved in a
    source checkout: an installed wheel found nothing and silently analysed
    every style as absent.
    """

    def test_default_path_lives_inside_the_package(self) -> None:
        from suno_dl import network_styles

        package_dir = Path(network_styles.__file__).parent
        assert network_styles.DEFAULT_CATALOGUE_PATH.parent == package_dir / "data"
        assert network_styles.DEFAULT_CATALOGUE_PATH.name == "genres.json"

    def test_default_catalogue_is_readable(self) -> None:
        from suno_dl.network_styles import DEFAULT_CATALOGUE_PATH

        assert DEFAULT_CATALOGUE_PATH.is_file(), DEFAULT_CATALOGUE_PATH
        data = load_catalogue()
        assert len(data["songs"]) == 247
        assert len(data["genres"]) == 10

    def test_packaged_copy_matches_the_test_fixture(self) -> None:
        from suno_dl.network_styles import DEFAULT_CATALOGUE_PATH

        assert DEFAULT_CATALOGUE_PATH.read_bytes() == CATALOGUE_PATH.read_bytes()

    def test_package_data_declares_the_catalogue(self) -> None:
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert '"data/*.json"' in pyproject
