from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from .models import Coverage, Digest, Post


@dataclass(frozen=True, slots=True)
class DigestOptions:
    timezone: ZoneInfo
    content_mode: str = "snippet"
    max_items: int = 20
    snippet_chars: int = 120
    max_message_chars: int = 6000
    url_template: str | None = None
    allowed_hosts: frozenset[str] = frozenset()


def build_digest(
    posts: list[Post] | tuple[Post, ...],
    *,
    coverage: Coverage,
    created_at: datetime,
    options: DigestOptions,
    batch_id: str | None = None,
) -> Digest:
    if options.content_mode not in {"snippet", "links_only"}:
        raise ValueError("content_mode 必须是 snippet 或 links_only")
    if options.max_items < 0 or options.snippet_chars <= 0 or options.max_message_chars <= 0:
        raise ValueError("简报长度配置无效")
    ordered = sorted(_dedupe(posts), key=lambda post: int(post.id), reverse=True)
    total = len(ordered)
    title = f"树洞雷达｜{total} 条新帖"
    header = _header(ordered, coverage, options.timezone)
    # max_items=0 是显式的“不按条数截断”；消息总长度仍由下方预算保护。
    max_shown = total if options.max_items == 0 else min(total, options.max_items)
    include_snippet = options.content_mode == "snippet"

    selected_count = max_shown
    content = ""
    while selected_count >= 0:
        for with_snippet in [True, False] if include_snippet else [False]:
            candidate = _render(
                header=header,
                posts=ordered[:selected_count],
                total=total,
                with_snippet=with_snippet,
                options=options,
            )
            if len(title) + len(candidate) <= options.max_message_chars:
                content = candidate
                break
        if content:
            break
        selected_count -= 1
    if not content:
        raise ValueError("max_message_chars 太小，无法容纳简报元数据")
    return Digest(
        batch_id=batch_id or uuid.uuid4().hex,
        title=title,
        content=content,
        # post_ids 代表整个通知批次，而不是只代表正文中展开的条目；未展开帖子也
        # 必须与该批次关联，避免下轮因长度限制再次被当成新帖。
        post_ids=tuple(post.id for post in ordered),
        created_at=created_at,
        coverage=coverage,
        post_count=total,
        shown_count=selected_count,
    )


def compact_text(text: str, *, has_media: bool = False, limit: int = 120) -> str:
    if not text.strip() and has_media:
        return "图片帖"
    clean = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def canonical_url(post: Post, options: DigestOptions) -> str:
    url = (
        options.url_template.replace("{id}", quote(post.id, safe=""))
        if options.url_template
        else post.url
    )
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in options.allowed_hosts
        or parsed.username
        or parsed.password
    ):
        raise ValueError("帖子链接不是受信任的 HTTPS 固定站点链接")
    return url


def _header(posts: list[Post], coverage: Coverage, timezone: ZoneInfo) -> str:
    if posts:
        times = [post.created_at.astimezone(timezone) for post in posts]
        start = min(times).strftime("%Y-%m-%d %H:%M:%S")
        end = max(times).strftime("%Y-%m-%d %H:%M:%S")
        time_text = start if start == end else f"{start} 至 {end}"
    else:
        time_text = "无"
    coverage_text = {
        Coverage.BASELINE: "baseline",
        Coverage.BOUNDED: "bounded（已到达旧边界）",
        Coverage.INCOMPLETE: "incomplete（预算内未到达旧边界）",
    }[coverage]
    return f"采集时间范围：{time_text}\n覆盖状态：{coverage_text}"


def _render(
    *,
    header: str,
    posts: list[Post],
    total: int,
    with_snippet: bool,
    options: DigestOptions,
) -> str:
    lines = [header]
    for post in posts:
        snippet = compact_text(post.text, has_media=post.has_media, limit=options.snippet_chars)
        line = f"\n#{post.id} {post.created_at.astimezone(options.timezone):%m-%d %H:%M}"
        if with_snippet and snippet:
            line += f"｜{snippet}"
        line += f"\n{canonical_url(post, options)}"
        lines.append(line)
    lines.append(f"\n本次共 {total} 条，展示 {len(posts)} 条，另 {total - len(posts)} 条未展开")
    return "".join(lines)


def _dedupe(posts: list[Post] | tuple[Post, ...]) -> list[Post]:
    result: list[Post] = []
    seen: set[str] = set()
    for post in posts:
        if post.id not in seen:
            result.append(post)
            seen.add(post.id)
    return result
