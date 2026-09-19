"""Offline, read-only rule replay; reports and private samples belong in ignored var/."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pku_hole_radar.attention import AttentionSettings, analyze_post
from pku_hole_radar.digest import DigestOptions, build_notification_digests
from pku_hole_radar.models import Comment, Coverage, Post

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_REVIEW = {
    "H004": "求职者求助，不是招聘机会；旧计划明确保留，但是否每次主动提醒可再决定。",
    "H005": "已经招满，只因涉及大模型而保留为交流；可能缺少时效价值。",
    "H006": "只泛泛提及大模型；没有拼成招聘机会，但交流内容本身信息量有限。",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def baseline_module(root: Path):
    spec = importlib.util.spec_from_file_location(
        "pku_hole_radar._review_baseline", root / "src" / "pku_hole_radar" / "attention.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("无法加载旧版关注规则")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cell(value: str, *, limit: int | None = None) -> str:
    value = value.replace("\n", " / ").replace("\r", "")
    if limit is not None and len(value) > limit:
        value = value[: limit - 1] + "…"
    return value.replace("|", "\\|").replace("`", "\\`")


def history_review(sample: dict) -> tuple[str, str]:
    if sample["id"] in HISTORICAL_REVIEW:
        return "历史边界待确认", HISTORICAL_REVIEW[sample["id"]]
    category = sample["old_category"]
    if category in {"实习线索", "科研线索"}:
        return "建议保留", "有已确认关注的技术方向及招募语境；不据关键词保证岗位真实或仍有效。"
    if category == "相关交流":
        return "建议保留", "相关技术领域的求职经验，符合此前兼顾经验交流的要求。"
    return "继续排除", "无关注方向或职业议题；其中部分为去重、长度和错误恢复用的占位正文。"


def evaluate(args) -> list[dict]:
    old = baseline_module(args.baseline_root)
    old_settings = old.AttentionSettings(enabled=True, preferred_locations=("深圳", "远程"))
    new_settings = AttentionSettings(
        enabled=True, career_enabled=True, preferred_locations=("深圳", "远程")
    )
    samples = []
    for sample in load_json(args.history):
        review, judgment = history_review(sample)
        samples.append({**sample, "group": "历史", "review": review, "judgment": judgment})
    for sample in load_json(args.private_samples):
        samples.append(
            {
                **sample,
                "group": "用户原帖",
                "sources": ["用户本轮提供的原文，仅保存在本地"],
                "judgment": "用户明确反馈感兴趣，应纳入。",
            }
        )
    for sample in load_json(ROOT / "tests" / "fixtures" / "career_attention.json")["samples"]:
        samples.append(
            {
                **sample,
                "group": "新增合成",
                "sources": ["tests/fixtures/career_attention.json"],
                "judgment": sample["reason"],
            }
        )

    now = datetime(2026, 9, 19, tzinfo=UTC)
    for index, sample in enumerate(samples, 1):
        candidate = Post(
            id=str(index),
            created_at=now,
            text=sample["text"],
            has_media=bool(sample.get("has_media", False)),
            url=f"https://fixture.test/post/{index}",
        )
        previous = old.analyze_post(candidate, old_settings)
        result = analyze_post(candidate, new_settings)
        sample.update(
            {
                "old_category": previous.category.value,
                "new_category": result.category.value,
                "old_relevant": previous.relevant,
                "new_relevant": result.relevant,
                "keywords": list(result.matched_keywords),
                "topics": list(result.topics),
                "evidence": result.excerpt,
                "reason": result.reason
                or (
                    f"方向：{result.direction}；命中词：{'、'.join(result.matched_keywords)}"
                    if result.relevant
                    else "未满足独立关注主题的证据组合"
                ),
            }
        )
    return samples


def render(samples: list[dict]) -> str:
    lines = [
        "# 职业发展关注规则：逐条审阅",
        "",
        "日期：2026-09-19。状态：离线规则回放，不代表当前部署状态；"
        "实际生效版本请以运行目录、配置和定时任务检查为准。",
        "",
        "口径：旧规则使用已发布代码；新规则同时开启原关注功能和 career_enabled。"
        "这里的“纳入”指会作为关注帖触发通知并展开全文/回复，不是基础采集是否成功。",
        "",
        "历史集合来自修改前 86 项测试中实际构造的所有 Post 正文，以及两份旧 fixture；"
        "同正文和媒体标记去重后 45 条。回复不作为分类输入。跨集合保留重复记录，以便追溯来源。",
        "",
        "## 实测汇总",
        "",
        "| 样本组 | 条数 | 旧规则纳入 | 新规则纳入 | 新增纳入 | 移出 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in ("用户原帖", "历史", "新增合成"):
        rows = [sample for sample in samples if sample["group"] == group]
        lines.append(
            f"| {group} | {len(rows)} | {sum(s['old_relevant'] for s in rows)} | "
            f"{sum(s['new_relevant'] for s in rows)} | "
            f"{sum(s['new_relevant'] and not s['old_relevant'] for s in rows)} | "
            f"{sum(s['old_relevant'] and not s['new_relevant'] for s in rows)} |"
        )
    lines += [
        "",
        "测试断言通过表示实现符合已写下的规则，不代表用户认可所有命中。"
        "5 条用户原帖及 B05（公务员与事业单位待遇比较）已获用户明确的正向反馈；"
        "其余合成样本的纳入/排除判断是我的建议。",
        "",
        "本轮已确认：B05 纳入职业发展，补充公务员、事业单位/事业编、体制内及国央企表述。"
        "其余待确认边界保持上一轮结果，不把这次确认视为对全部边界的认可。",
        "",
        "## 需要你决定的边界",
        "",
        "下表明确列出候选规则的实际结果；不会把它们计作用户已确认的正确分类。",
        "",
        "| 编号 | 内容 | 当前结果 | 人工复核 |",
        "| --- | --- | --- | --- |",
    ]
    for s in samples:
        if "待确认" in s["review"]:
            lines.append(
                f"| {s['id']} | {cell(s['text'])} | {s['new_category']} | {cell(s['judgment'])} |"
            )
    lines += [
        "",
        "我的建议：保留技术职业、相关行业的待遇与就业讨论；产品岗和柜员比较可保留供你决策；"
        "父母退休待遇及医生/律师就业可能过宽；秋招情绪、已招满和仅提及方向的帖子需要你决定是否值得主动提醒。",
        "",
        "## 所有被纳入的样本",
        "",
        "完整原文、命中句和判断依据见下面逐条记录；表内长句仅为导航摘要。",
        "",
        "| 编号 | 来源 | 旧 → 新 | 内容摘要 | 人工判断 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in samples:
        if s["new_relevant"]:
            lines.append(
                f"| {s['id']} | {s['group']} | {s['old_category']} → {s['new_category']} | "
                f"{cell(s['text'], limit=90)} | {s['review']} |"
            )
    lines += ["", "## 全部样本逐条记录", ""]
    for s in samples:
        lines += [
            f"### {s['id']} · {s['group']} · {s['new_category']}",
            "",
            f"旧规则：{s['old_category']}；新规则：{s['new_category']}；人工标注：{s['review']}。",
            "",
            "原文：",
            "",
            "```text",
            s["text"] or "[无文字正文]",
            "```",
            "",
            f"程序依据：{s['reason']}",
            "",
            f"人工复核：{s['judgment']}",
            "",
        ]
        if s["evidence"]:
            lines += ["命中句：", "", "```text", s["evidence"], "```", ""]
        lines += ["来源：" + "；".join(f"`{source}`" for source in s["sources"]), ""]
    return "\n".join(lines)


def write_notification_preview(output: Path) -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    candidate = Post(
        "1", now, "收到一家国企的offer。税前25万值得去吗？", "https://fixture.test/post/1"
    )
    parts = build_notification_digests(
        [candidate],
        comments_by_post={
            "1": [
                Comment("11", "1", now, "合成回复：可以一起比较工作强度、薪酬结构与岗位发展。"),
                Comment("12", "1", now, "合成回复：谢谢，我也想了解晋升路径。", is_author=True),
            ]
        },
        coverage=Coverage.BOUNDED,
        created_at=now,
        options=DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            attention=AttentionSettings(enabled=True, career_enabled=True),
            allowed_hosts=frozenset({"fixture.test"}),
            max_message_chars=6000,
        ),
    )
    lines = ["# 通知预览（全为合成内容，未发送）", ""]
    for part in parts:
        lines += [f"标题：{part.title}", "", "```text", part.content, "```", ""]
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--private-samples", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var" / "attention-review")
    args = parser.parse_args()
    samples = evaluate(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "review.md").write_text(render(samples), encoding="utf-8")
    (args.output_dir / "results.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_notification_preview(args.output_dir / "notification-preview.md")
    mismatches = [
        s["id"] for s in samples if s.get("expected", s["new_category"]) != s["new_category"]
    ]
    print(
        json.dumps(
            {
                "records": len(samples),
                "unique_texts": len({s["text"] for s in samples}),
                "included": sum(s["new_relevant"] for s in samples),
                "by_group": {
                    group: dict(Counter(s["new_category"] for s in samples if s["group"] == group))
                    for group in ("用户原帖", "历史", "新增合成")
                },
                "mismatches": mismatches,
                "report": str(args.output_dir / "review.md"),
            },
            ensure_ascii=False,
        )
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
