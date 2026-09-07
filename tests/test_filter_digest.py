from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from pku_hole_radar.digest import DigestOptions, build_digest, canonical_url
from pku_hole_radar.filtering import KeywordFilter, normalize_text
from pku_hole_radar.models import Coverage, Post


def post(post_id: int, text: str, *, media: bool = False, url: str | None = None) -> Post:
    return Post(
        id=str(post_id),
        created_at=datetime(2026, 9, 5, 0, 0, tzinfo=UTC),
        text=text,
        url=url or f"https://fixture.test/post/{post_id}",
        has_media=media,
    )


def options(**overrides: object) -> DigestOptions:
    values: dict[str, object] = {
        "timezone": ZoneInfo("Asia/Shanghai"),
        "content_mode": "snippet",
        "max_items": 20,
        "snippet_chars": 120,
        "max_message_chars": 6000,
        "allowed_hosts": frozenset({"fixture.test"}),
    }
    values.update(overrides)
    return DigestOptions(**values)  # type: ignore[arg-type]


def test_keyword_filter_normalizes_unicode_and_exclude_wins() -> None:
    assert normalize_text("ＰＫＵ") == "pku"
    matcher = KeywordFilter(include_any=("ＰＫＵ", "树洞"), exclude_any=("广告",))
    assert matcher.matches("我在 pku 看到新帖")
    assert matcher.matches("关于树洞的讨论")
    assert not matcher.matches("PKU 广告")
    assert KeywordFilter().matches("任意正文")
    with pytest.raises(ValueError, match="空白"):
        KeywordFilter(include_any=(" ",))


def test_digest_caps_items_keeps_omitted_count_and_stores_all_ids() -> None:
    posts = [post(100 + index, "x" * 500) for index in range(25)]
    digest = build_digest(
        posts,
        coverage=Coverage.INCOMPLETE,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=options(max_message_chars=900),
    )
    assert digest.post_count == 25
    assert digest.shown_count < 25
    assert len(digest.post_ids) == 25
    assert "本次共 25 条" in digest.content
    assert "未展开" in digest.content
    assert len(digest.title) + len(digest.content) <= 900
    assert "incomplete" in digest.content


def test_unlimited_items_show_every_post_within_message_budget() -> None:
    posts = [post(200 + index, "x" * 120) for index in range(30)]
    digest = build_digest(
        posts,
        coverage=Coverage.BOUNDED,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=options(max_items=0, max_message_chars=20000),
    )
    assert digest.post_count == 30
    assert digest.shown_count == 30
    assert len(digest.post_ids) == 30
    assert "本次共 30 条，展示 30 条，另 0 条未展开" in digest.content


def test_links_only_and_image_post_do_not_leak_arbitrary_url_or_media() -> None:
    digest = build_digest(
        [post(123, "secret body", media=True, url="https://evil.example/123?token=leak")],
        coverage=Coverage.BOUNDED,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=options(
            content_mode="links_only",
            url_template="https://treehole.pku.edu.cn/p/ {id}".replace(" ", ""),
            allowed_hosts=frozenset({"treehole.pku.edu.cn"}),
        ),
    )
    assert "secret body" not in digest.content
    assert "token=leak" not in digest.content
    assert "https://treehole.pku.edu.cn/p/123" in digest.content
    assert "图片帖" not in digest.content


def test_untrusted_url_is_rejected_without_fixed_template() -> None:
    bad = post(123, "x", url="https://evil.example/123")
    with pytest.raises(ValueError, match="受信任"):
        canonical_url(bad, options())


def test_trusted_query_template_is_preserved() -> None:
    result = canonical_url(
        post(123, "x"),
        options(
            url_template="https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid={id}",
            allowed_hosts=frozenset({"treehole.pku.edu.cn"}),
        ),
    )
    assert result == "https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid=123"
