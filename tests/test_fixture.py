from __future__ import annotations

from pathlib import Path

from pku_hole_radar.source import FixtureSource


def test_fixture_is_local_and_uses_one_based_tokens() -> None:
    source = FixtureSource.from_file(Path(__file__).parent / "fixtures" / "basic.json")
    page = source.fetch_page(None, 30)
    assert [post.id for post in page.posts] == ["105", "104", "103"]
    assert page.exhausted is True
    assert source.calls == [(None, 30)]
