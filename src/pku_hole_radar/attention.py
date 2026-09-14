from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .models import Post

DEFAULT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "llm4se": (
        "LLM4SE",
        "AI4SE",
        "代码大模型",
        "软件工程大模型",
        "代码智能",
        "智能软件工程",
        "coding agent",
        "code agent",
        "software engineering agent",
        "编程智能体",
        "SWE-bench",
        "SWE-agent",
        "mini-swe-agent",
    ),
    "software_engineering": (
        "代码生成",
        "代码补全",
        "程序修复",
        "自动修复",
        "测试生成",
        "单元测试生成",
        "缺陷定位",
        "代码审查",
        "仓库级",
        "repository-level",
        "program repair",
        "test generation",
        "code review",
    ),
    "llm": (
        "大模型",
        "大语言模型",
        "大型语言模型",
        "语言大模型",
        "LLM",
        "LLMs",
        "large language model",
        "large language models",
    ),
    "agent": (
        "智能体",
        "AI Agent",
        "AI Agents",
        "LLM Agent",
        "多智能体",
        "multi-agent",
        "agentic",
        "coding agent",
        "code agent",
        "Agent",
        "Agents",
    ),
    "internship": (
        "实习",
        "实习生",
        "日常实习",
        "暑期实习",
        "研究实习",
        "科研实习",
        "算法实习",
        "intern",
        "internship",
        "research intern",
    ),
    "research": (
        "研究助理",
        "科研助理",
        "RA",
        "research assistant",
        "助研",
        "科研合作",
        "课题组",
        "实验室",
        "进组",
    ),
    "recruitment": (
        "招聘",
        "招募",
        "招收",
        "招人",
        "急招",
        "诚招",
        "在招",
        "招聘实习生",
        "招研究助理",
        "招RA",
        "招 RA",
        "hiring",
        "we are hiring",
    ),
    "supporting_recruitment": (
        "内推",
        "HC",
        "岗位",
        "投递",
        "简历",
        "到岗",
        "工作地点",
        "base",
    ),
    "help_seeking": (
        "求实习",
        "找实习",
        "求内推",
        "求捞",
        "有没有内推",
        "求科研",
        "求进组",
        "找导师",
    ),
    "experience": (
        "面经",
        "面试经验",
        "实习体验",
        "实习经验",
        "科研经验",
        "论文投稿",
        "审稿意见",
        "投稿经验",
        "复现",
        "经验分享",
        "避坑",
    ),
    "shenzhen": ("深圳", "Shenzhen"),
    "remote": (
        "远程实习",
        "可远程",
        "支持远程",
        "接受远程",
        "remote internship",
        "fully remote",
        "remote",
    ),
    "secondary": (
        "RAG",
        "检索增强",
        "工具调用",
        "function calling",
        "tool use",
        "强化学习",
        "RL",
        "SFT",
        "微调",
        "推理优化",
        "长上下文",
        "多模态",
        "智能软件工程评测",
    ),
}

SUPPORTED_PREFERRED_LOCATIONS = ("深圳", "远程")

GROUP_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "llm4se": "LLM4SE",
        "software_engineering": "软件工程任务",
        "llm": "大模型",
        "agent": "Agent",
        "internship": "实习",
        "research": "科研",
        "recruitment": "招募",
        "supporting_recruitment": "岗位信息",
        "help_seeking": "求助",
        "experience": "经验交流",
        "shenzhen": "深圳",
        "remote": "远程",
        "secondary": "AI 技术",
    }
)


class AttentionCategory(StrEnum):
    INTERNSHIP = "实习线索"
    RESEARCH = "科研线索"
    EXCHANGE = "相关交流"
    OTHER = "其他"


@dataclass(frozen=True, slots=True)
class AttentionSettings:
    """通知摘要的本地规则配置。

    规则只用于解释、排序和摘录，不改变 runner 的采集候选范围。
    """

    enabled: bool = False
    title_max_chars: int = 40
    max_title_categories: int = 2
    preferred_locations: tuple[str, ...] = ()
    keywords: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_KEYWORDS))

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("attention.enabled 必须是布尔值")
        if (
            isinstance(self.title_max_chars, bool)
            or not isinstance(self.title_max_chars, int)
            or not 20 <= self.title_max_chars <= 100
        ):
            raise ValueError("attention.title_max_chars 必须是 20 到 100 之间的整数")
        if (
            isinstance(self.max_title_categories, bool)
            or not isinstance(self.max_title_categories, int)
            or not 1 <= self.max_title_categories <= 3
        ):
            raise ValueError("attention.max_title_categories 必须是 1 到 3 之间的整数")

        locations: list[str] = []
        for location in self.preferred_locations:
            if not isinstance(location, str):
                raise ValueError("attention.preferred_locations 必须是字符串数组")
            normalized = normalize_match_text(location)
            if normalized not in {"深圳", "远程"}:
                raise ValueError("attention.preferred_locations 只能包含 深圳 或 远程")
            if normalized not in locations:
                locations.append(normalized)

        if not isinstance(self.keywords, Mapping):
            raise ValueError("attention.keywords 必须是 TOML 表")
        normalized_groups: dict[str, tuple[str, ...]] = {}
        for group, values in self.keywords.items():
            if group not in DEFAULT_KEYWORDS:
                raise ValueError(f"attention.keywords 包含未知分组：{group}")
            if not isinstance(values, list | tuple):
                raise ValueError(f"attention.keywords.{group} 必须是字符串数组")
            normalized_values: list[str] = []
            seen: set[str] = set()
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"attention.keywords.{group} 不允许空白关键词")
                normalized = normalize_match_text(value)
                if normalized and normalized not in seen:
                    normalized_values.append(value.strip())
                    seen.add(normalized)
            normalized_groups[group] = tuple(normalized_values)

        for group, values in DEFAULT_KEYWORDS.items():
            normalized_groups.setdefault(group, values)

        object.__setattr__(self, "preferred_locations", tuple(locations))
        object.__setattr__(self, "keywords", MappingProxyType(normalized_groups))


@dataclass(frozen=True, slots=True)
class AttentionMatch:
    post_id: str
    category: AttentionCategory
    direction: str | None
    matched_groups: tuple[str, ...]
    matched_keywords: tuple[str, ...]
    location: str | None
    excerpt: str
    direction_score: int
    location_rank: int

    @property
    def relevant(self) -> bool:
        return self.category != AttentionCategory.OTHER

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        category_rank = {
            AttentionCategory.INTERNSHIP: 0,
            AttentionCategory.RESEARCH: 1,
            AttentionCategory.EXCHANGE: 2,
            AttentionCategory.OTHER: 3,
        }[self.category]
        return (category_rank, -self.direction_score, self.location_rank, -int(self.post_id))


@dataclass(frozen=True, slots=True)
class _Occurrence:
    group: str
    keyword: str
    start: int
    end: int
    normalized_keyword: str


@dataclass(frozen=True, slots=True)
class _Segment:
    start: int
    end: int
    occurrences: tuple[_Occurrence, ...]


@dataclass(frozen=True, slots=True)
class _SegmentResult:
    start: int
    end: int
    category: AttentionCategory
    direction: str | None
    direction_score: int
    location: str | None
    location_rank: int
    occurrences: tuple[_Occurrence, ...]


def normalize_match_text(value: str) -> str:
    """用于规则匹配的 NFKC、大小写和空白规范化。"""

    normalized, _ = _normalize_with_mapping(value)
    return normalized


def analyze_post(
    post: Post, settings: AttentionSettings, *, excerpt_chars: int = 120
) -> AttentionMatch:
    if not settings.enabled:
        return _other_match(post.id)
    occurrences = _find_occurrences(post.text, settings.keywords)
    if not occurrences:
        return _other_match(post.id)

    segments = _split_segments(post.text, occurrences)
    candidates = [_analyze_segment(post.text, segment, settings) for segment in segments]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        return _other_match(post.id)

    selected = min(
        candidates,
        key=lambda candidate: (
            {
                AttentionCategory.INTERNSHIP: 0,
                AttentionCategory.RESEARCH: 1,
                AttentionCategory.EXCHANGE: 2,
                AttentionCategory.OTHER: 3,
            }[candidate.category],
            -candidate.direction_score,
            candidate.location_rank,
        ),
    )
    matched_groups = _unique(
        occurrence.group for occurrence in selected.occurrences if occurrence.group in GROUP_LABELS
    )
    matched_keywords = _unique(occurrence.keyword for occurrence in selected.occurrences)
    excerpt = _excerpt(
        post.text[selected.start : selected.end],
        selected.occurrences,
        limit=excerpt_chars,
        source_offset=selected.start,
    )
    return AttentionMatch(
        post_id=post.id,
        category=selected.category,
        direction=selected.direction,
        matched_groups=matched_groups,
        matched_keywords=matched_keywords,
        location=selected.location,
        excerpt=excerpt,
        direction_score=selected.direction_score,
        location_rank=selected.location_rank,
    )


def rank_posts(
    posts: Sequence[Post], settings: AttentionSettings, *, excerpt_chars: int = 120
) -> tuple[tuple[Post, AttentionMatch], ...]:
    """分析并按“类别、方向、地点、ID”稳定排序。"""

    analyzed = [(post, analyze_post(post, settings, excerpt_chars=excerpt_chars)) for post in posts]
    return tuple(sorted(analyzed, key=lambda item: item[1].sort_key))


def _other_match(post_id: str) -> AttentionMatch:
    return AttentionMatch(
        post_id=post_id,
        category=AttentionCategory.OTHER,
        direction=None,
        matched_groups=(),
        matched_keywords=(),
        location=None,
        excerpt="",
        direction_score=0,
        location_rank=999,
    )


def _analyze_segment(
    text: str, segment: _Segment, settings: AttentionSettings
) -> _SegmentResult | None:
    raw = text[segment.start : segment.end]
    occurrences = tuple(
        occurrence for occurrence in segment.occurrences if _valid_occurrence(occurrence, raw)
    )
    if not occurrences:
        return None

    group_names = {occurrence.group for occurrence in occurrences}
    has_llm4se = "llm4se" in group_names
    has_llm = "llm" in group_names
    has_agent = "agent" in group_names
    has_software_task = "software_engineering" in group_names
    has_secondary = "secondary" in group_names
    has_internship = "internship" in group_names
    has_research = "research" in group_names
    has_role_or_context = (
        has_internship
        or has_research
        or bool(group_names & {"recruitment", "supporting_recruitment", "experience"})
    )
    if has_software_task and not (has_llm4se or has_llm or has_agent):
        has_software_task = False
    if has_secondary and not (has_llm4se or has_llm or has_agent or has_role_or_context):
        has_secondary = False
    direction_score = 0
    direction: str | None = None
    if has_llm4se:
        direction_score = 3
        direction = GROUP_LABELS["llm4se"]
    elif has_software_task and (has_llm or has_agent):
        direction_score = 2
        direction = GROUP_LABELS["software_engineering"]
    elif has_llm or has_agent:
        direction_score = 2
        direction = GROUP_LABELS["llm"] if has_llm else GROUP_LABELS["agent"]
    elif has_secondary:
        direction_score = 1
        direction = "AI 技术"

    has_role = has_internship or has_research
    negative_recruitment = _contains_negative_recruitment(raw)
    active_recruitment = (
        has_role
        and not negative_recruitment
        and _has_active_recruitment(
            raw,
            occurrences,
            has_role,
            short_form_enabled=bool(settings.keywords.get("recruitment")),
        )
    )

    location = _location_evidence(raw, segment, occurrences, settings)
    if active_recruitment and has_internship and direction_score:
        category = AttentionCategory.INTERNSHIP
    elif active_recruitment and has_research and direction_score:
        category = AttentionCategory.RESEARCH
    elif direction_score:
        category = AttentionCategory.EXCHANGE
    else:
        category = AttentionCategory.OTHER

    if category == AttentionCategory.OTHER and not direction_score:
        return None
    location_rank = _location_rank(location, settings.preferred_locations)
    return _SegmentResult(
        start=segment.start,
        end=segment.end,
        category=category,
        direction=direction,
        direction_score=direction_score,
        location=location,
        location_rank=location_rank,
        occurrences=occurrences,
    )


def _has_active_recruitment(
    raw: str,
    occurrences: Sequence[_Occurrence],
    has_role: bool,
    *,
    short_form_enabled: bool,
) -> bool:
    if any(occurrence.group == "recruitment" for occurrence in occurrences):
        return True
    normalized = normalize_match_text(raw)
    if not has_role:
        return False
    # “招 RA / 招ＲＡ”一类短写法不依赖把单字“招”加入可配置词库；
    # 只在其后紧跟角色词时视为当前招募。
    if short_form_enabled and re.search(
        r"(?<!不)(?<!已)招(?:募|收)?(?=.{0,12}(?:实习|intern|ra|研究助理|科研助理|人))",
        normalized,
    ):
        return True

    auxiliary = {
        occurrence.normalized_keyword
        for occurrence in occurrences
        if occurrence.group == "supporting_recruitment"
    }
    has_job_description = any(
        marker in normalized
        for marker in ("职责", "负责", "要求", "工作内容", "岗位描述", "岗位职责", "薪资")
    )
    return len(auxiliary) >= 2 and has_job_description


def _contains_negative_recruitment(raw: str) -> bool:
    normalized = normalize_match_text(raw)
    return any(
        phrase in normalized
        for phrase in (
            "不招",
            "暂停招聘",
            "已招满",
            "招满",
            "停止招聘",
            "不再招聘",
            "hc冻结",
            "hc 冻结",
            "not hiring",
            "no openings",
            "position filled",
            "岗位已满",
            "招聘已结束",
        )
    )


def _location_evidence(
    raw: str,
    segment: _Segment,
    occurrences: Sequence[_Occurrence],
    settings: AttentionSettings,
) -> str | None:
    remote_denied = _contains_negative_remote(raw)
    evidence: dict[str, bool] = {"深圳": False, "远程": False}
    for occurrence in occurrences:
        if occurrence.group == "shenzhen" and not _historical_location(
            raw, occurrence, segment.start
        ):
            evidence["深圳"] = True
        if (
            occurrence.group == "remote"
            and not remote_denied
            and _valid_remote(raw, occurrence, segment.start, occurrences)
        ):
            evidence["远程"] = True
    location_order = settings.preferred_locations or SUPPORTED_PREFERRED_LOCATIONS
    for location in location_order:
        if evidence.get(location):
            return location
    return None


def _historical_location(raw: str, occurrence: _Occurrence, segment_start: int) -> bool:
    local_start = max(0, occurrence.start - segment_start)
    local_end = max(local_start, occurrence.end - segment_start)
    before = normalize_match_text(raw[max(0, local_start - 14) : local_start])
    after = normalize_match_text(raw[local_end : local_end + 18])
    return any(
        marker in before or marker in after
        for marker in ("读过", "就读", "毕业", "曾在", "以前", "之前", "来自", "本科")
    )


def _contains_negative_remote(raw: str) -> bool:
    normalized = normalize_match_text(raw)
    return any(
        phrase in normalized
        for phrase in (
            "不支持远程",
            "不可远程",
            "不接受远程",
            "仅线下",
            "仅限线下",
            "not remote",
            "on-site only",
            "onsite only",
        )
    )


def _valid_remote(
    raw: str,
    occurrence: _Occurrence,
    segment_start: int,
    occurrences: Sequence[_Occurrence],
) -> bool:
    normalized = normalize_match_text(raw)
    local_start = max(0, occurrence.start - segment_start)
    local_end = max(local_start, occurrence.end - segment_start)
    keyword = occurrence.normalized_keyword
    if keyword == "remote":
        after = normalized[local_end : local_end + 24]
        if re.match(r"\s+(?:repository|repo|desktop|server|branch|access)", after):
            return False
        has_role_or_recruitment = any(
            item.group in {"internship", "research", "recruitment", "supporting_recruitment"}
            for item in occurrences
        )
        if not has_role_or_recruitment:
            return False
    return True


def _valid_occurrence(occurrence: _Occurrence, raw: str) -> bool:
    if occurrence.group != "agent":
        return True
    if occurrence.normalized_keyword not in {"agent", "agents"}:
        return True
    context = normalize_match_text(raw)
    if "user-agent" in context or "user agent" in context:
        return False
    technical_markers = (
        "大模型",
        "大语言模型",
        "人工智能",
        "代码",
        "编程",
        "软件",
        "科研",
        "研究",
        "算法",
        "实习",
        "招聘",
        "llm",
        "ai",
    )
    return any(
        marker in context
        if not marker.isascii()
        else bool(re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", context))
        for marker in technical_markers
    )


def _location_rank(location: str | None, preferred_locations: Sequence[str]) -> int:
    if location is None:
        return len(preferred_locations)
    try:
        return preferred_locations.index(location)
    except ValueError:
        return len(preferred_locations)


def _split_segments(text: str, occurrences: Sequence[_Occurrence]) -> tuple[_Segment, ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n\r]+", text):
        end = match.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    if not spans and text.strip():
        spans.append((0, len(text)))

    result: list[_Segment] = []
    for start, end in spans:
        result.append(
            _Segment(
                start=start,
                end=end,
                occurrences=tuple(
                    occurrence for occurrence in occurrences if start <= occurrence.start < end
                ),
            )
        )
    return tuple(result)


def _find_occurrences(text: str, keywords: Mapping[str, Sequence[str]]) -> tuple[_Occurrence, ...]:
    normalized, source_map = _normalize_with_mapping(text)
    if not normalized:
        return ()
    found: list[_Occurrence] = []
    seen: set[tuple[str, str, int, int]] = set()
    for group, values in keywords.items():
        for keyword in values:
            normalized_keyword = normalize_match_text(keyword)
            if not normalized_keyword:
                continue
            if _is_ascii_term(normalized_keyword):
                pattern = re.compile(rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])")
                matches = pattern.finditer(normalized)
            else:
                matches = _substring_matches(normalized, normalized_keyword)
            for match in matches:
                match_start, match_end = _match_bounds(match)
                start = source_map[match_start]
                end = source_map[match_end - 1] + 1
                key = (group, normalized_keyword, start, end)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    _Occurrence(
                        group=group,
                        keyword=keyword.strip(),
                        start=start,
                        end=end,
                        normalized_keyword=normalized_keyword,
                    )
                )
    return tuple(sorted(found, key=lambda item: (item.start, item.end, item.group, item.keyword)))


def _substring_matches(text: str, needle: str):
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return
        yield _Match(index, index + len(needle))
        start = index + 1


@dataclass(frozen=True, slots=True)
class _Match:
    start: int
    end: int


def _match_bounds(match) -> tuple[int, int]:
    if isinstance(match, _Match):
        return match.start, match.end
    return match.start(), match.end()


def _is_ascii_term(value: str) -> bool:
    return value.isascii() and all(
        character.isalnum() or character in {" ", "-", "_"} for character in value
    )


def _normalize_with_mapping(value: str) -> tuple[str, tuple[int, ...]]:
    chars: list[str] = []
    sources: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for item in normalized:
            chars.append(" " if item.isspace() else item)
            sources.append(index)

    collapsed: list[str] = []
    collapsed_sources: list[int] = []
    in_space = False
    for character, source in zip(chars, sources, strict=True):
        if character == " ":
            if collapsed and not in_space:
                collapsed.append(character)
                collapsed_sources.append(source)
            in_space = True
        else:
            collapsed.append(character)
            collapsed_sources.append(source)
            in_space = False

    left = 1 if collapsed and collapsed[0] == " " else 0
    right = len(collapsed) - 1 if collapsed and collapsed[-1] == " " else len(collapsed)
    return "".join(collapsed[left:right]), tuple(collapsed_sources[left:right])


def _clean_with_mapping(value: str) -> tuple[str, tuple[int, ...]]:
    chars: list[str] = []
    sources: list[int] = []
    in_space = False
    for index, character in enumerate(value):
        if unicodedata.category(character) == "Cc" and character not in "\n\t":
            continue
        if character.isspace():
            if chars and not in_space:
                chars.append(" ")
                sources.append(index)
            in_space = True
        else:
            chars.append(character)
            sources.append(index)
            in_space = False
    while chars and chars[0] == " ":
        chars.pop(0)
        sources.pop(0)
    while chars and chars[-1] == " ":
        chars.pop()
        sources.pop()
    return "".join(chars), tuple(sources)


def _excerpt(
    segment_text: str,
    occurrences: Sequence[_Occurrence],
    *,
    limit: int,
    source_offset: int = 0,
) -> str:
    if limit <= 0:
        return ""
    clean, source_map = _clean_with_mapping(segment_text)
    if not clean:
        return ""
    positions: list[tuple[int, int]] = []
    for occurrence in occurrences:
        relative_start = max(0, occurrence.start - source_offset)
        relative_end = max(relative_start, occurrence.end - source_offset)
        mapped = [
            index
            for index, source in enumerate(source_map)
            if relative_start <= source < relative_end
        ]
        if mapped:
            positions.append((mapped[0], mapped[-1] + 1))
    if not positions:
        return clean[:limit]

    first = min(start for start, _ in positions)
    last = max(end for _, end in positions)
    if last - first <= limit:
        start = max(0, min(first - limit // 3, len(clean) - limit))
        end = min(len(clean), start + limit)
        return _window(clean, start, end, limit)

    # 招募词和方向词相距很远时，保留两个有解释力的原文短段。
    half = max(1, (limit - 1) // 2)
    first_part = _centered_window(clean, first, half)
    second_part = _centered_window(clean, last, limit - len(first_part) - 1)
    if not second_part:
        return first_part[:limit]
    return f"{first_part}…{second_part}"[:limit]


def _centered_window(text: str, center: int, limit: int) -> str:
    if limit <= 0:
        return ""
    start = max(0, min(center - limit // 3, len(text) - limit))
    end = min(len(text), start + limit)
    return _window(text, start, end, limit)


def _window(text: str, start: int, end: int, limit: int) -> str:
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    available = max(1, limit - len(prefix) - len(suffix))
    fragment = text[start:end]
    if len(fragment) > available:
        fragment = fragment[:available]
    return f"{prefix}{fragment}{suffix}"[:limit]


def _unique(values) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return tuple(result)
