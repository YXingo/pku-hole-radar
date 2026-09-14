from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pku_hole_radar.attention import (
    AttentionCategory,
    AttentionSettings,
    analyze_post,
    rank_posts,
)
from pku_hole_radar.config import ConfigError, load_config
from pku_hole_radar.digest import DigestOptions, build_digest
from pku_hole_radar.models import Coverage, Post, SendResult, SendState
from pku_hole_radar.store import Store


def post(post_id: int, text: str) -> Post:
    return Post(
        id=str(post_id),
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        text=text,
        url=f"https://fixture.test/post/{post_id}",
    )


def settings(**overrides: object) -> AttentionSettings:
    values: dict[str, object] = {
        "enabled": True,
        "preferred_locations": ("深圳", "远程"),
    }
    values.update(overrides)
    return AttentionSettings(**values)  # type: ignore[arg-type]


def test_attention_classifies_recruitment_help_and_closed_posts() -> None:
    assert (
        analyze_post(post(1, "深圳团队招聘 coding agent 实习生，欢迎投递简历"), settings()).category
        == AttentionCategory.INTERNSHIP
    )
    research = analyze_post(post(2, "课题组招募 LLM 研究助理，支持远程"), settings())
    assert research.category == AttentionCategory.RESEARCH
    assert research.location == "远程"

    help_seeking = analyze_post(post(3, "求深圳大模型实习，有没有内推"), settings())
    assert help_seeking.category == AttentionCategory.EXCHANGE
    assert analyze_post(post(4, "本组大模型实习生已招满"), settings()).category == (
        AttentionCategory.EXCHANGE
    )


def test_attention_does_not_stitch_unrelated_segments_or_history() -> None:
    unrelated = analyze_post(post(1, "A 段谈大模型；B 段招聘无关运营实习"), settings())
    assert unrelated.category == AttentionCategory.EXCHANGE
    assert unrelated.direction == "大模型"

    history = analyze_post(post(2, "深圳读过本科，现在北京团队招聘 LLM 实习生"), settings())
    assert history.category == AttentionCategory.INTERNSHIP
    assert history.location is None

    resolved = analyze_post(post(3, "求实习已解决。我们组现在招聘大模型实习生"), settings())
    assert resolved.category == AttentionCategory.INTERNSHIP


def test_attention_uses_full_text_and_ascii_boundaries() -> None:
    long_text = "前置说明 " * 40 + "深圳团队招聘 LLM 实习生，欢迎投递"
    result = analyze_post(post(1, long_text), settings(), excerpt_chars=60)
    assert result.category == AttentionCategory.INTERNSHIP
    assert "招聘" in result.excerpt
    assert "LLM" in result.excerpt

    full_width = analyze_post(post(2, "招ＲＡ，研究方向为ＬＬＭ，欢迎投递"), settings())
    assert full_width.category == AttentionCategory.RESEARCH
    assert "ＲＡ" in full_width.excerpt
    noise = analyze_post(post(3, "train smallmodel User-Agent remote repository"), settings())
    assert noise.category == AttentionCategory.OTHER


def test_attention_ranking_is_category_direction_location_then_id() -> None:
    posts = [
        post(1, "北京团队招聘大模型实习生"),
        post(2, "深圳团队招聘 coding agent 实习生"),
        post(3, "课题组招募 LLM 研究助理，支持远程"),
        post(4, "大模型实习面经，分享面试经验"),
    ]
    ranked = rank_posts(posts, settings())
    assert [item[0].id for item in ranked] == ["2", "1", "3", "4"]


def test_attention_digest_counts_whole_batch_and_links_only_does_not_leak_text() -> None:
    posts = [
        post(1, "深圳团队招聘 coding agent 实习生，欢迎投递简历"),
        post(2, "课题组招募 LLM 研究助理，支持远程"),
        post(3, "普通树洞 secret body"),
    ]
    digest = build_digest(
        posts,
        coverage=Coverage.INCOMPLETE,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=settings(),
            max_items=1,
            max_message_chars=2000,
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    )
    assert digest.title.startswith("实习1·科研1")
    assert "关注概览（关键词规则）：实习线索 1，科研线索 1" in digest.content
    assert digest.post_ids == ("1", "2", "3")
    assert digest.shown_count == 1
    assert "incomplete" in digest.content

    links_only = build_digest(
        posts,
        coverage=Coverage.BOUNDED,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=settings(),
            content_mode="links_only",
            max_items=0,
            max_message_chars=2000,
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    )
    assert "coding agent" not in links_only.title
    assert "coding agent" not in links_only.content
    assert "LLM4SE" not in links_only.content
    assert "secret body" not in links_only.content
    assert "命中句" not in links_only.content


def test_attention_title_includes_a_short_excerpt_only_for_one_category() -> None:
    digest = build_digest(
        [
            post(1, "深圳团队招聘 coding agent 实习生，欢迎投递简历"),
            post(2, "普通树洞"),
        ],
        coverage=Coverage.BOUNDED,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=settings(title_max_chars=32),
            max_items=1,
            max_message_chars=2000,
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    )
    assert digest.title.startswith("实习1｜")
    assert "招聘" in digest.title
    assert len(digest.title) <= 32

    no_match = build_digest(
        [post(3, "普通树洞")],
        coverage=Coverage.BOUNDED,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=settings(),
            max_message_chars=2000,
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    )
    assert no_match.title == "树洞雷达｜1帖·关注0"


def test_attention_outbox_retry_reuses_saved_title_and_content() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    candidate = post(1, "深圳团队招聘 coding agent 实习生，欢迎投递简历")
    digest = build_digest(
        [candidate],
        coverage=Coverage.BOUNDED,
        created_at=now,
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=settings(),
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    )
    with Store(":memory:") as store:
        store.commit_collection(
            now=now,
            posts=[candidate],
            matched_ids={candidate.id},
            digest=digest,
            coverage=Coverage.BOUNDED,
            proposed_watermark=None,
        )
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        claim = store.claim_outbox(
            now=now,
            daily_limit=10,
            day_start=day_start,
            day_end=day_start + timedelta(days=1),
            pending_ttl=timedelta(days=1),
        ).claim
        assert claim is not None
        store.finish_send(
            claim.attempt_id,
            now,
            SendResult(SendState.FAILED, error_message="fixture failure"),
        )
        store.retry_outbox(digest.batch_id, now, acknowledge_duplicate=False)
        retried = store.claim_outbox(
            now=now,
            daily_limit=10,
            day_start=day_start,
            day_end=day_start + timedelta(days=1),
            pending_ttl=timedelta(days=1),
        ).claim
        assert retried is not None
        assert retried.title == digest.title
        assert retried.content == digest.content


def test_attention_configuration_defaults_can_be_overridden(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[app]
timezone = "Asia/Shanghai"
state_dir = "state"
secrets_file = ".env"

[source]

[filters]

[attention]
enabled = true
title_max_chars = 32
max_title_categories = 1
preferred_locations = ["深圳"]

[attention.keywords]
llm4se = ["自定义方向"]
agent = []

[notify]
provider = "stdout"
channel = "wechat"
content_mode = "snippet"
max_items = 20
snippet_chars = 120
max_message_chars = 6000
max_send_attempts_per_day = 60
pending_ttl_hours = 24
send_retry_spacing_seconds = 1800
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.attention.enabled is True
    assert config.attention.title_max_chars == 32
    assert config.attention.keywords["llm4se"] == ("自定义方向",)
    assert config.attention.keywords["llm"]
    assert config.attention.keywords["agent"] == ()

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preferred_locations = ["深圳"]', 'preferred_locations = ["广州"]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="只能包含 深圳 或 远程"):
        load_config(config_path)
