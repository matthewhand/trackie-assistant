"""Fixture-specific page consistency research, not proof of producer identity.

The displayed metadata and Flight data can be generated from the same unknown
source. A match is useful for investigation, but cannot authorize an automated
mapping or bypass production guards. Tests use one static public-page fixture.
"""

from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from research_checks.test_flight_route_research import _fixture, _observed_clip, _replace


class UnmatchedPage(ValueError):
    pass


def _fixture_agrees(html: str, rows: tuple, clip_id: str) -> bool:
    """Check the fixture's captured rendering pattern, with no trust claim."""
    soup = BeautifulSoup(html, "html.parser")
    canonical = [
        link.get("href")
        for link in soup.find_all("link", href=True)
        if "canonical" in link.get_attribute_list("rel")
    ]
    titles = soup.find_all("title")
    descriptions = soup.find_all("meta", attrs={"name": "description"})
    if (
        canonical != [f"https://suno.com/song/{clip_id}"]
        or len(titles) != 1
        or len(descriptions) != 1
    ):
        raise UnmatchedPage("ambiguous or mismatched document identity")
    clip = _observed_clip(rows, clip_id)
    title, display_name, handle = (
        clip.get("title"),
        clip.get("display_name"),
        clip.get("handle"),
    )
    if not all(isinstance(value, str) and value for value in (title, display_name, handle)):
        raise UnmatchedPage("missing observed fields")
    expected_title = f"{title} by {display_name} | Suno"
    expected_description = (
        f"{title} by {display_name} (@{handle}). Listen and make your own on Suno."
    )
    if (
        titles[0].get_text() != expected_title
        or descriptions[0].get("content") != expected_description
    ):
        raise UnmatchedPage("fixture render fields do not agree")
    return True


def _page() -> str:
    return (Path(__file__).parents[1] / "tests" / "fixtures" / "real_song_page.html").read_text()


def test_captured_page_render_matches_observed_route_fields():
    rows, clip_id = _fixture()
    assert _fixture_agrees(_page(), rows, clip_id)


@pytest.mark.parametrize(
    "changed", ["canonical", "duplicate_canonical", "duplicate_description", "title"]
)
def test_page_disagreement_or_ambiguity_is_rejected(changed):
    rows, clip_id = _fixture()
    soup = BeautifulSoup(_page(), "html.parser")
    if changed == "canonical":
        soup.find("link", rel="canonical")["href"] = "https://suno.com/song/other"
    elif changed == "duplicate_canonical":
        link = soup.new_tag("link", rel="canonical", href=f"https://suno.com/song/{clip_id}")
        soup.head.append(link)
    elif changed == "duplicate_description":
        meta = soup.new_tag("meta", attrs={"name": "description", "content": "ambiguous"})
        soup.head.append(meta)
    else:
        soup.title.string = "conflicting title"
    with pytest.raises(UnmatchedPage):
        _fixture_agrees(str(soup), rows, clip_id)


def test_consistent_replacement_is_not_independent_producer_proof():
    rows, clip_id = _fixture()
    element = deepcopy(next(row.value for row in rows if row.row_id == 0x40))
    element[3]["clip"].update(display_name="Unproven Name", handle="unproven_handle")
    altered_rows = _replace(rows, 0x40, element)
    title = element[3]["clip"]["title"]
    soup = BeautifulSoup(_page(), "html.parser")
    soup.title.string = f"{title} by Unproven Name | Suno"
    soup.find("meta", attrs={"name": "description"})["content"] = (
        f"{title} by Unproven Name (@unproven_handle). Listen and make your own on Suno."
    )
    # The same consistency test accepts both tuples. It proves neither account
    # created this clip; an independent provider contract/evidence is missing.
    assert _fixture_agrees(_page(), rows, clip_id)
    assert _fixture_agrees(str(soup), altered_rows, clip_id)
