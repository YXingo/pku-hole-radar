from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pku_hole_radar.attention import AttentionCategory, AttentionSettings, analyze_post, rank_posts
from pku_hole_radar.config import ConfigError, load_config
from pku_hole_radar.digest import DigestOptions, build_notification_digests
from pku_hole_radar.models import Comment, Coverage, Post

SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "career_attention.json").read_text(encoding="utf-8")
)["samples"]
NOW = datetime(2026, 9, 19, tzinfo=UTC)


def post(text: str, pid: str = "1") -> Post:
    return Post(pid, NOW, text, f"https://fixture.test/post/{pid}")


def settings(**kwargs) -> AttentionSettings:
    return AttentionSettings(enabled=True, career_enabled=True, **kwargs)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["id"])
def test_career_corpus(sample) -> None:
    result = analyze_post(post(sample["text"]), settings())
    assert result.category.value == sample["expected"], result
    if result.category == AttentionCategory.CAREER:
        assert result.reason
        assert result.topics
        assert result.excerpt


def test_career_is_explicitly_opt_in_and_master_switch_still_wins() -> None:
    candidate = post("国企薪资怎么样？")
    assert not analyze_post(candidate, AttentionSettings(enabled=True)).relevant
    assert not analyze_post(candidate, AttentionSettings(career_enabled=True)).relevant
    assert analyze_post(candidate, settings()).relevant
    with pytest.raises(ValueError, match="career_enabled"):
        AttentionSettings(career_enabled="yes")


def test_career_word_groups_can_be_disabled_and_overridden() -> None:
    assert not analyze_post(
        post("国企薪资怎么样？"), settings(keywords={"career_employer": (), "career_role": ()})
    ).relevant
    assert analyze_post(
        post("某单位薪资怎么样？"), settings(keywords={"career_employer": ("某单位",)})
    ).relevant
    assert not analyze_post(
        post("国企薪资怎么样？"), settings(keywords={"career_pay": ()})
    ).relevant
    assert not analyze_post(
        post("校招offer怎么选？"), settings(keywords={"career_choice": ()})
    ).relevant
    assert not analyze_post(
        post("算法工程师被AI取代？"), settings(keywords={"career_displacement": ()})
    ).relevant


def test_career_config_accepts_switch_and_four_title_categories(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[attention]\nenabled = true\ncareer_enabled = true\nmax_title_categories = 4\n"
        '[attention.keywords]\ncareer_employer = ["某单位"]\n',
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert loaded.attention.career_enabled
    assert loaded.attention.max_title_categories == 4
    assert analyze_post(post("某单位薪资如何？"), loaded.attention).relevant
    config.write_text('[attention]\ncareer_enabled = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="career_enabled"):
        load_config(config)


def test_career_reason_covers_adjacent_sentences_and_original_excerpt() -> None:
    candidate = post("收到一家国企的offer。税前25万值得去吗？")
    result = analyze_post(candidate, settings())
    assert "相邻承接句" in result.reason
    assert "国企" in result.excerpt and "税前" in result.excerpt
    assert "薪资待遇" in result.topics


def test_career_ranks_after_opportunities_and_before_technical_exchange() -> None:
    rows = [
        post("大模型最近有什么研究？", "5"),
        post("国企薪资怎么样？", "4"),
        post("招聘 coding agent 实习生，薪资可谈", "3"),
        post("课题组招募 LLM 研究助理，薪酬可谈", "2"),
        post("普通树洞", "1"),
    ]
    assert [item.id for item, _ in rank_posts(rows, settings())] == ["3", "2", "4", "5", "1"]


def test_career_full_details_and_links_only_privacy() -> None:
    candidate = post("国企薪资怎么样？想了解完整待遇。")
    replies = {"1": [Comment("11", "1", NOW, "完整回复：这是合成薪资说明。", is_author=True)]}
    for mode in ("snippet", "links_only"):
        parts = build_notification_digests(
            [candidate],
            comments_by_post=replies,
            coverage=Coverage.BOUNDED,
            created_at=NOW,
            options=DigestOptions(
                timezone=ZoneInfo("Asia/Shanghai"),
                attention=settings(),
                content_mode=mode,
                max_message_chars=6000,
                allowed_hosts=frozenset({"fixture.test"}),
            ),
        )
        assert len(parts) == 1
        assert parts[0].title.startswith("职业1")
        assert "职业发展 1" in parts[0].content
        if mode == "snippet":
            assert candidate.text in parts[0].content
            assert replies["1"][0].text in parts[0].content
            assert "纳入理由：" in parts[0].content
            assert "关注主题：薪资待遇" in parts[0].content
        else:
            assert "国企" not in parts[0].title + parts[0].content
            assert "薪资" not in parts[0].content
            assert replies["1"][0].text not in parts[0].content
