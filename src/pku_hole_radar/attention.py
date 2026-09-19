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
    "career_employer": (
        "互联网",
        "大厂",
        "央国企",
        "国央企",
        "央企",
        "国企",
        "事业单位",
        "体制内",
        "银行",
        "制造业",
        "科技公司",
        "民企",
        "外企",
        "字节",
        "腾讯",
        "阿里",
        "华为",
        "美团",
        "百度",
        "拼多多",
        "快手",
        "小米",
        "京东",
        "微软",
        "谷歌",
    ),
    "career_role": (
        "算法岗",
        "算法工程师",
        "程序员",
        "开发岗",
        "开发工程师",
        "软件工程师",
        "技术岗",
        "研发岗",
        "测试开发",
        "数据分析师",
        "公务员",
        "事业编",
    ),
    "career_pay": (
        "薪资",
        "薪酬",
        "薪水",
        "待遇",
        "工资",
        "年薪",
        "月薪",
        "起薪",
        "年包",
        "总包",
        "税前",
        "税后",
        "年终奖",
        "调薪",
        "涨薪",
        "降薪",
    ),
    "career_workload": (
        "工作强度",
        "加班",
        "996",
        "双休",
        "单休",
        "work-life balance",
        "wlb",
    ),
    "career_outlook": (
        "就业",
        "找工作",
        "找到工作",
        "找不到工作",
        "毕业去向",
        "秋招",
        "春招",
        "校招",
        "社招",
        "职业规划",
        "职业发展",
        "职业路径",
        "职业选择",
        "发展空间",
        "晋升",
        "转行",
        "跳槽",
        "岗位内容",
        "裁员",
        "失业",
        "中年危机",
        "职业寿命",
        "35岁",
        "还有饭吃",
    ),
    "career_tradeoff": ("怎么选", "如何选", "选哪个", "值得去", "值不值得去", "要不要去"),
    "career_choice": (
        "offer怎么选",
        "offer 怎么选",
        "选offer",
        "选 offer",
        "offer选择",
        "offer 选择",
        "毕业找不到工作",
        "毕业后找不到工作",
        "毕业还能找到工作",
        "秋招形势",
        "校招形势",
        "秋招行情",
        "校招行情",
        "秋招焦虑",
        "校招焦虑",
        "就业前景",
        "就业市场",
        "职业规划",
    ),
    "career_displacement": ("取代", "替代", "淘汰"),
}

SUPPORTED_PREFERRED_LOCATIONS = ("深圳", "远程")
CAREER_GROUPS = frozenset(group for group in DEFAULT_KEYWORDS if group.startswith("career_"))
CAREER_TOPICS = {
    "career_pay": "薪资待遇",
    "career_workload": "工作强度",
    "career_outlook": "就业与发展",
    "career_tradeoff": "岗位选择",
    "career_choice": "求职择业",
    "career_displacement": "职业替代风险",
}

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
        "career_employer": "行业与单位",
        "career_role": "职业岗位",
        **CAREER_TOPICS,
    }
)


class AttentionCategory(StrEnum):
    INTERNSHIP = "实习线索"
    RESEARCH = "科研线索"
    CAREER = "职业发展"
    EXCHANGE = "相关交流"
    OTHER = "其他"


CATEGORY_ORDER = {category: index for index, category in enumerate(AttentionCategory)}


@dataclass(frozen=True, slots=True)
class AttentionSettings:
    """通知摘要的本地规则配置。

    规则只用于解释、排序和摘录，不改变 runner 的采集候选范围。
    """

    enabled: bool = False
    career_enabled: bool = False
    title_max_chars: int = 40
    max_title_categories: int = 2
    preferred_locations: tuple[str, ...] = ()
    keywords: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_KEYWORDS))

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("attention.enabled 必须是布尔值")
        if not isinstance(self.career_enabled, bool):
            raise ValueError("attention.career_enabled 必须是布尔值")
        if (
            isinstance(self.title_max_chars, bool)
            or not isinstance(self.title_max_chars, int)
            or not 20 <= self.title_max_chars <= 100
        ):
            raise ValueError("attention.title_max_chars 必须是 20 到 100 之间的整数")
        if (
            isinstance(self.max_title_categories, bool)
            or not isinstance(self.max_title_categories, int)
            or not 1 <= self.max_title_categories <= 4
        ):
            raise ValueError("attention.max_title_categories 必须是 1 到 4 之间的整数")

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
    topics: tuple[str, ...] = ()
    reason: str = ""

    @property
    def relevant(self) -> bool:
        return self.category != AttentionCategory.OTHER

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        category_rank = CATEGORY_ORDER[self.category]
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
    topics: tuple[str, ...] = ()
    reason: str = ""


def normalize_match_text(value: str) -> str:
    """用于规则匹配的 NFKC、大小写和空白规范化。"""

    normalized, _ = _normalize_with_mapping(value)
    return normalized


def analyze_post(
    post: Post, settings: AttentionSettings, *, excerpt_chars: int = 120
) -> AttentionMatch:
    if not settings.enabled:
        return _other_match(post.id)
    keywords = {
        group: values
        for group, values in settings.keywords.items()
        if settings.career_enabled or group not in CAREER_GROUPS
    }
    occurrences = _find_occurrences(post.text, keywords)
    if not occurrences:
        return _other_match(post.id)

    segments = _split_segments(post.text, occurrences)
    candidates = [_analyze_segment(post.text, segment, settings) for segment in segments]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if settings.career_enabled:
        candidates.extend(_career_candidates(post.text, segments, settings))
    if not candidates:
        return _other_match(post.id)

    selected = min(
        candidates,
        key=lambda candidate: (
            CATEGORY_ORDER[candidate.category],
            -candidate.direction_score,
            -len(candidate.topics),
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
        topics=selected.topics,
        reason=selected.reason,
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
        occurrence
        for occurrence in segment.occurrences
        if occurrence.group not in CAREER_GROUPS and _valid_occurrence(occurrence, raw)
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


def _career_candidates(
    text: str, segments: Sequence[_Segment], settings: AttentionSettings
) -> list[_SegmentResult]:
    """职业主题与技术方向独立；只在一句或明确承接的相邻两句内组合证据。"""

    results: list[_SegmentResult] = []
    for index, segment in enumerate(segments):
        result = _analyze_career_segment(text, segment, settings, adjacent=False)
        if result is not None:
            results.append(result)
        if index == 0:
            continue
        previous = segments[index - 1]
        if not _career_continuation(text, previous, segment):
            continue
        combined = _Segment(previous.start, segment.end, previous.occurrences + segment.occurrences)
        result = _analyze_career_segment(text, combined, settings, adjacent=True)
        if result is not None:
            results.append(result)
    return results


def _career_continuation(text: str, previous: _Segment, current: _Segment) -> bool:
    left = text[previous.start : previous.end]
    right = text[current.start : current.end]
    if re.search(r"\n\s*\n|\r\n\s*\r\n|[;；]", left + right):
        return False
    if len(left) + len(right) > 240:
        return False
    # “银行转账失败。工资不够花”不能借用前句的银行；前句本身须有就业语境。
    normalized_left = normalize_match_text(left)
    # 短标题“国企\n薪资怎么样？”属于明确承接；正文中的零散提及不享受这个例外。
    object_heading = any(
        item.group in {"career_employer", "career_role"}
        and normalized_left.rstrip("。！？!?：:") == item.normalized_keyword
        for item in previous.occurrences
    )
    has_employment_context = (
        any(
            token in normalized_left
            for token in ("工作", "入职", "岗位", "秋招", "校招", "实习", "招聘", "offer")
        )
        or any(item.group == "career_role" for item in previous.occurrences)
        or object_heading
    )
    if not has_employment_context:
        return False
    # 只允许待遇等承接句和指代承接，显式转话题、独立主体或一般碎碎念不跨句拼接。
    normalized_right = normalize_match_text(right).lstrip()
    return normalized_right.startswith(
        (
            "税前",
            "税后",
            "工资",
            "薪资",
            "薪酬",
            "待遇",
            "年薪",
            "月薪",
            "年包",
            "总包",
            "起薪",
            "加班",
            "工作强度",
            "发展空间",
            "晋升",
            "这家",
            "那家",
            "这个岗位",
            "这个岗",
            "这个行业",
            "那边",
            "这里",
            "这份工作",
            "它的",
        )
    )


def _analyze_career_segment(
    text: str, segment: _Segment, settings: AttentionSettings, *, adjacent: bool
) -> _SegmentResult | None:
    raw = text[segment.start : segment.end]
    normalized = normalize_match_text(raw)
    occurrences = tuple(
        item
        for item in segment.occurrences
        if item.group in CAREER_GROUPS and _career_literal_is_valid(text, item)
    )
    # 同一个“央国企”不重复解释成“央国企、国企”，保留该组最长的原文证据。
    occurrences = tuple(
        item
        for item in occurrences
        if not any(
            other.group == item.group
            and other != item
            and other.start <= item.start
            and other.end >= item.end
            for other in occurrences
        )
    )
    objects = tuple(
        item for item in occurrences if item.group in {"career_employer", "career_role"}
    )
    choices = tuple(item for item in occurrences if item.group == "career_choice")
    has_role = any(item.group == "career_role" for item in objects)
    has_job_context = has_role or any(
        marker in normalized for marker in ("岗位", "入职", "工作", "校招", "秋招", "招聘")
    )
    if (
        not objects
        and not has_job_context
        and any(
            marker in normalized
            for marker in ("留学", "录取", "保研", "夏令营", "申请硕士", "申请博士")
        )
    ):
        choices = tuple(item for item in choices if "offer" not in item.normalized_keyword)
    issues = tuple(
        item
        for item in occurrences
        if item.group in {"career_pay", "career_workload", "career_outlook"}
        or (item.group == "career_tradeoff" and has_job_context)
    )
    # 银行交易、消费和股票分析并非职业待遇；明确技术/岗位语境仍可被单独识别。
    if objects and all(item.normalized_keyword == "银行" for item in objects):
        if any(word in normalized for word in ("转账", "银行卡", "开户", "存款", "取款")):
            objects = ()
    if not has_job_context and any(
        word in normalized for word in ("股票", "股价", "市值", "估值", "利润")
    ):
        issues = tuple(item for item in issues if item.normalized_keyword != "发展空间")
    risks = tuple(
        item
        for item in occurrences
        if item.group == "career_displacement" and _job_displacement(raw, objects, item)
    )
    if not choices and not (objects and (issues or risks)):
        return None
    evidence = tuple(sorted({*objects, *issues, *choices, *risks}, key=lambda item: item.start))
    topics = _unique(CAREER_TOPICS[item.group] for item in evidence if item.group in CAREER_TOPICS)
    if objects and (issues or risks):
        names = "、".join(_unique(item.keyword for item in objects))
        context = "相邻承接句" if adjacent else "同一句"
        reason = f"职业对象「{names}」与「{'、'.join(topics)}」在{context}中关联"
    else:
        names = "、".join(_unique(item.keyword for item in choices))
        reason = f"明确求职择业表达「{names}」"
    return _SegmentResult(
        start=segment.start,
        end=segment.end,
        category=AttentionCategory.CAREER,
        direction=None,
        direction_score=0,
        location=None,
        location_rank=_location_rank(None, settings.preferred_locations),
        occurrences=evidence,
        topics=topics,
        reason=reason,
    )


def _career_literal_is_valid(text: str, occurrence: _Occurrence) -> bool:
    """局部排除词形相同但并非职业对象/待遇的用法，不做全帖排除。"""

    after = normalize_match_text(text[occurrence.end : occurrence.end + 16])
    before = normalize_match_text(text[max(0, occurrence.start - 8) : occurrence.start])
    if occurrence.normalized_keyword == "字节" and after.startswith(("码", "数", "长度", "序")):
        return False
    if occurrence.group == "career_pay":
        if after.startswith(("字段", "变量", "组件", "接口", "查询", "表查询", "表的sql", "利润")):
            return False
        if any(marker in before for marker in ("字段名", "变量名", "字段为", "字段叫")):
            return False
    return True


def _job_displacement(raw: str, objects: Sequence[_Occurrence], risk: _Occurrence) -> bool:
    """被取代的是职业角色，而不是程序员讨论的排序算法、代码或产品。"""

    normalized = normalize_match_text(raw)
    for role in objects:
        if role.group != "career_role":
            continue
        role_text = re.escape(role.normalized_keyword)
        risk_text = re.escape(risk.normalized_keyword)
        passive = (
            rf"{role_text}\s*(?:岗位|职位|这个职业)?\s*"
            rf"(?:会不会|是否会|会|可能会|可能|容易|将会|将|要)?\s*被\s*"
            rf"(?:(?:ai|人工智能|自动化|大模型|机器人|机器|新人|应届生|年轻人|外包)\s*)?"
            rf"(?:逐渐|完全|部分|彻底|大规模)?{risk_text}"
        )
        active = (
            rf"(?<![a-z0-9])(?:ai|人工智能|自动化|大模型).{{0,12}}{risk_text}"
            rf"\s*(?:所有的|所有|大部分|部分|初级|高级)?\s*{role_text}"
        )
        if re.search(passive, normalized) or re.search(active, normalized):
            return True
    return False


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
