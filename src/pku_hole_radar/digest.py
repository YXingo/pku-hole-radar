from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from .attention import AttentionCategory, AttentionMatch, AttentionSettings, rank_posts
from .models import Comment, Coverage, Digest, Post


@dataclass(frozen=True, slots=True)
class DigestOptions:
    timezone: ZoneInfo
    content_mode: str = "snippet"
    max_items: int = 20
    snippet_chars: int = 120
    max_message_chars: int = 6000
    url_template: str | None = None
    allowed_hosts: frozenset[str] = frozenset()
    attention: AttentionSettings | None = None


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
    attention_matches: dict[str, AttentionMatch] | None = None
    attention_enabled = options.attention is not None and options.attention.enabled
    if attention_enabled:
        ranked = rank_posts(ordered, options.attention, excerpt_chars=options.snippet_chars)
        ordered = [post for post, _match in ranked]
        attention_matches = {post.id: match for post, match in ranked}
        attention_enabled = any(match.relevant for _post, match in ranked)
        title = (
            _attention_title(ranked, total=total, options=options)
            if attention_enabled
            else f"树洞雷达｜{total} 条新帖"
        )
    else:
        title = f"树洞雷达｜{total} 条新帖"
    header = _header(ordered, coverage, options.timezone)
    # max_items=0 是显式的“不按条数截断”；消息总长度仍由下方预算保护。
    max_shown = total if options.max_items == 0 else min(total, options.max_items)
    include_snippet = options.content_mode == "snippet"

    selected_count = max_shown
    content = ""
    while selected_count >= 0:
        for with_snippet in [True, False] if include_snippet else [False]:
            if attention_enabled and attention_matches is not None:
                candidate = _render_attention(
                    header=header,
                    posts=[(post, attention_matches[post.id]) for post in ordered[:selected_count]],
                    all_posts=ranked,
                    total=total,
                    with_snippet=with_snippet,
                    options=options,
                )
            else:
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


def build_notification_digests(
    posts: Sequence[Post],
    *,
    comments_by_post: Mapping[str, Sequence[Comment]],
    coverage: Coverage,
    created_at: datetime,
    options: DigestOptions,
) -> tuple[Digest, ...]:
    """构造可投递通知；关注帖全文与全部可见回复不因单条渠道上限而截断。"""

    ordered = sorted(_dedupe(tuple(posts)), key=lambda post: int(post.id), reverse=True)
    settings = options.attention
    if options.content_mode == "links_only" or settings is None or not settings.enabled:
        return (build_digest(ordered, coverage=coverage, created_at=created_at, options=options),)

    ranked = rank_posts(ordered, settings, excerpt_chars=options.snippet_chars)
    focused = [(post, match) for post, match in ranked if match.relevant]
    if not focused:
        return (build_digest(ordered, coverage=coverage, created_at=created_at, options=options),)

    missing = [post.id for post, _match in focused if post.id not in comments_by_post]
    if missing:
        raise ValueError("关注帖缺少完整回复结果")

    total = len(ordered)
    title = _attention_title(ranked, total=total, options=options)
    blocks = [
        _header(ordered, coverage, options.timezone),
        _attention_overview(ranked, total=total),
        "【优先关注｜原帖与回复全文】",
    ]
    for index, (post, match) in enumerate(focused, 1):
        blocks.append(
            _render_full_attention_post(
                post,
                match,
                comments=comments_by_post[post.id],
                index=index,
                focused_count=len(focused),
                options=options,
            )
        )

    ordinary = [(post, match) for post, match in ranked if not match.relevant]
    if options.max_items == 0:
        shown_ordinary = ordinary
    else:
        shown_ordinary = ordinary[: max(0, options.max_items - len(focused))]
    if shown_ordinary:
        blocks.append("【其他新帖】")
        blocks.extend(_render_ordinary_post(post, options) for post, _match in shown_ordinary)
    shown_count = len(focused) + len(shown_ordinary)
    blocks.append(
        f"本批累计共 {total} 条，关注 {len(focused)} 条，展示 {shown_count} 条，"
        f"另 {total - shown_count} 条未展开"
    )
    content = "\n\n".join(blocks)
    return _multipart_digests(
        title=title,
        content=content,
        posts=ordered,
        coverage=coverage,
        created_at=created_at,
        shown_count=shown_count,
        max_message_chars=options.max_message_chars,
    )


def _render_full_attention_post(
    post: Post,
    match: AttentionMatch,
    *,
    comments: Sequence[Comment],
    index: int,
    focused_count: int,
    options: DigestOptions,
) -> str:
    lines = [
        f"━━ 关注帖 {index}/{focused_count}｜{match.category.value} ━━",
        f"#{post.id}｜{post.created_at.astimezone(options.timezone):%Y-%m-%d %H:%M:%S}",
    ]
    details = []
    if match.direction:
        details.append(f"方向：{match.direction}")
    if match.location:
        details.append(f"地点：{match.location}")
    if details:
        lines.append("｜".join(details))
    if match.matched_keywords:
        lines.append("命中词：" + "、".join(match.matched_keywords))
    lines.extend(
        (
            "原帖全文：",
            _full_text(post.text, has_media=post.has_media, empty_label="无文字原帖"),
            f"原帖链接：{canonical_url(post, options)}",
        )
    )

    ordered_comments = sorted(
        comments,
        key=lambda comment: (comment.created_at, int(comment.id)),
    )
    lines.append(f"回复（{len(ordered_comments)} 条，已完整获取当前公开可见回复）：")
    if not ordered_comments:
        lines.append("暂无公开回复")
    for floor, comment in enumerate(ordered_comments, 1):
        label = "｜洞主" if comment.is_author else ""
        quote_label = f"｜回复 C{comment.quote_id}" if comment.quote_id else ""
        lines.append(
            f"— {floor} 楼｜C{comment.id}｜"
            f"{comment.created_at.astimezone(options.timezone):%m-%d %H:%M:%S}"
            f"{label}{quote_label} —"
        )
        lines.append(
            _full_text(comment.text, has_media=comment.has_media, empty_label="无文字回复")
        )
    return "\n".join(lines)


def _render_ordinary_post(post: Post, options: DigestOptions) -> str:
    line = f"#{post.id}｜{post.created_at.astimezone(options.timezone):%m-%d %H:%M}"
    snippet = compact_text(post.text, has_media=post.has_media, limit=options.snippet_chars)
    if snippet:
        line += f"｜{snippet}"
    return f"{line}\n{canonical_url(post, options)}"


def _full_text(text: str, *, has_media: bool, empty_label: str) -> str:
    clean = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")
    clean = clean.replace("\r\n", "\n").replace("\r", "\n").strip()
    if clean:
        if has_media:
            return clean + "\n[含图片，图片请在原帖中查看]"
        return clean
    if has_media:
        return f"[{empty_label}；含图片，请在原帖中查看]"
    return f"[{empty_label}]"


def _multipart_digests(
    *,
    title: str,
    content: str,
    posts: Sequence[Post],
    coverage: Coverage,
    created_at: datetime,
    shown_count: int,
    max_message_chars: int,
) -> tuple[Digest, ...]:
    group_id = uuid.uuid4().hex
    if len(title) + len(content) <= max_message_chars:
        return (
            Digest(
                batch_id=group_id,
                group_id=group_id,
                title=title,
                content=content,
                post_ids=tuple(post.id for post in posts),
                created_at=created_at,
                coverage=coverage,
                post_count=len(posts),
                shown_count=shown_count,
            ),
        )

    # 为标题分片后缀和正文分片标记预留固定空间；正文按换行优先切分且不丢字符。
    chunk_limit = max_message_chars - len(title) - 48
    if chunk_limit < 80:
        raise ValueError("max_message_chars 太小，无法承载关注帖完整内容分片")
    chunks = _split_complete_text(content, chunk_limit)
    part_count = len(chunks)
    digests: list[Digest] = []
    for index, chunk in enumerate(chunks, 1):
        part_title = f"{title}（{index}/{part_count}）"
        part_content = f"第 {index}/{part_count} 部分\n{chunk}"
        if len(part_title) + len(part_content) > max_message_chars:
            raise ValueError("关注帖通知分片仍超过消息长度上限")
        digests.append(
            Digest(
                batch_id=f"{group_id}-{index:04d}",
                group_id=group_id,
                part_index=index,
                part_count=part_count,
                title=part_title,
                content=part_content,
                post_ids=tuple(post.id for post in posts) if index == 1 else (),
                created_at=created_at,
                coverage=coverage,
                post_count=len(posts),
                shown_count=shown_count if index == 1 else 0,
            )
        )
    return tuple(digests)


def _split_complete_text(text: str, limit: int) -> tuple[str, ...]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            newline = text.rfind("\n", start + limit // 2, end)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return tuple(chunks)


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


def _attention_title(
    ranked: list[tuple[Post, AttentionMatch]] | tuple[tuple[Post, AttentionMatch], ...],
    *,
    total: int,
    options: DigestOptions,
) -> str:
    settings = options.attention
    if settings is None or not settings.enabled:
        return f"树洞雷达｜{total} 条新帖"

    counts = {
        category: sum(1 for _post, match in ranked if match.category == category)
        for category in AttentionCategory
        if category != AttentionCategory.OTHER
    }
    categories = [category for category, count in counts.items() if count]
    categories = categories[: settings.max_title_categories]
    labels = {
        AttentionCategory.INTERNSHIP: "实习",
        AttentionCategory.RESEARCH: "科研",
        AttentionCategory.EXCHANGE: "交流",
    }
    if not categories:
        base = f"树洞雷达｜{total}帖·关注0"
        return _fit_title(base, max_chars=settings.title_max_chars)

    category_text = "·".join(f"{labels[category]}{counts[category]}" for category in categories)
    suffix = f"｜{total}帖"
    base = f"{category_text}{suffix}"
    if len(categories) == 1 and options.content_mode != "links_only":
        best = next(match for _post, match in ranked if match.category == categories[0])
        if best.excerpt:
            available = settings.title_max_chars - len(category_text) - len(suffix) - 1
            if available > 0:
                excerpt = _shorten_title_text(best.excerpt, available)
                candidate = f"{category_text}｜{excerpt}{suffix}"
                if len(candidate) <= settings.title_max_chars:
                    return candidate
    return _fit_title(base, max_chars=settings.title_max_chars)


def _fit_title(base: str, *, max_chars: int) -> str:
    if len(base) <= max_chars:
        return base
    # 先删除次要类别；数字和类别标签整体保留，避免标题变成半个计数。
    if "·" in base:
        category_text, suffix = base.split("｜", 1)
        parts = category_text.split("·")
        while len(parts) > 1:
            parts.pop()
            candidate = "·".join(parts) + "｜" + suffix
            if len(candidate) <= max_chars:
                return candidate
    if len(base) > max_chars:
        raise ValueError("attention.title_max_chars 太小，无法容纳简报计数")
    return base


def _shorten_title_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _render_attention(
    *,
    header: str,
    posts: list[tuple[Post, AttentionMatch]],
    all_posts: list[tuple[Post, AttentionMatch]] | tuple[tuple[Post, AttentionMatch], ...],
    total: int,
    with_snippet: bool,
    options: DigestOptions,
) -> str:
    lines = [header, _attention_overview(all_posts, total=total)]
    for post, match in posts:
        line = f"\n#{post.id} {post.created_at.astimezone(options.timezone):%m-%d %H:%M}"
        line += f"｜{match.category.value}"
        if options.content_mode != "links_only":
            if match.direction:
                line += f"｜方向：{match.direction}"
            if match.location:
                line += f"｜地点：{match.location}"
        if with_snippet:
            if match.relevant and match.excerpt:
                line += f"\n命中句：{match.excerpt}"
                if match.matched_keywords:
                    keywords = "、".join(match.matched_keywords[:4])
                    line += f"\n命中词：{keywords}"
            else:
                snippet = compact_text(
                    post.text,
                    has_media=post.has_media,
                    limit=options.snippet_chars,
                )
                if snippet:
                    line += f"｜{snippet}"
        line += f"\n{canonical_url(post, options)}"
        lines.append(line)
    lines.append(f"\n本次共 {total} 条，展示 {len(posts)} 条，另 {total - len(posts)} 条未展开")
    return "".join(lines)


def _attention_overview(
    posts: list[tuple[Post, AttentionMatch]] | tuple[tuple[Post, AttentionMatch], ...],
    *,
    total: int,
) -> str:
    # 这里仅展示主类别计数；links_only 不输出命中词、原文片段或其他正文信息。
    counts = {category: 0 for category in AttentionCategory}
    for _post, match in posts:
        counts[match.category] += 1
    other_count = total - sum(
        counts[category] for category in AttentionCategory if category != AttentionCategory.OTHER
    )
    return (
        "\n关注概览（关键词规则）："
        f"实习线索 {counts[AttentionCategory.INTERNSHIP]}，"
        f"科研线索 {counts[AttentionCategory.RESEARCH]}，"
        f"相关交流 {counts[AttentionCategory.EXCHANGE]}，"
        f"其他 {other_count}"
    )


def _dedupe(posts: list[Post] | tuple[Post, ...]) -> list[Post]:
    result: list[Post] = []
    seen: set[str] = set()
    for post in posts:
        if post.id not in seen:
            result.append(post)
            seen.add(post.id)
    return result
