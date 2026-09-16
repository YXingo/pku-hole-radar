from __future__ import annotations

import fcntl
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .attention import rank_posts
from .digest import DigestOptions, build_notification_digests
from .filtering import KeywordFilter
from .models import (
    Comment,
    CommentPage,
    Coverage,
    Digest,
    ErrorKind,
    FetchSummary,
    Page,
    Post,
    RunSummary,
    SendResult,
    SendState,
)
from .notifier import Notifier
from .source import Source, SourceError
from .store import ClaimDecision, Store, StoreError


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""

    def monotonic(self) -> float:
        """返回单调时钟秒数。"""

    def sleep(self, seconds: float) -> None:
        """等待指定秒数；测试时可推进虚拟时钟。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class LockBusy(RuntimeError):
    """已有进程持有同一状态目录的非阻塞锁。"""


class ProcessLock:
    """使用内核锁而不是“锁文件是否存在”实现进程互斥。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._handle: object | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockBusy(f"状态目录正在被另一个进程使用：{self.path.parent}") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    timezone: ZoneInfo
    interval_seconds: int = 1800
    page_size: int = 30
    max_pages: int = 3
    max_posts: int = 0
    max_requests: int = 0
    request_spacing_seconds: float = 2
    retry_wait_seconds: float = 5
    run_timeout_seconds: float = 180
    max_send_attempts: int = 3
    daily_send_limit: int = 60
    pending_ttl_hours: float = 24
    send_spacing_seconds: float = 13
    send_retry_spacing_seconds: float = 1800
    cleanup_interval_seconds: float = 86400
    cleanup_batch_size: int = 500
    push_when_post_count_exceeds: int = 0
    comment_page_size: int = 50
    digest_options: DigestOptions | None = None

    def __post_init__(self) -> None:
        if self.push_when_post_count_exceeds < 0:
            raise ValueError("push_when_post_count_exceeds 不能为负数")
        if self.comment_page_size <= 0:
            raise ValueError("comment_page_size 必须为正数")


@dataclass(slots=True)
class _Budget:
    started_mono: float
    deadline_mono: float
    request_count: int = 0
    pages: int = 0
    last_request_mono: float | None = None

    def remaining(self, current_mono: float) -> float:
        return self.deadline_mono - current_mono


@dataclass(frozen=True, slots=True)
class _SendOutcome:
    batch_id: str | None = None
    state: SendState | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None


class Runner:
    """执行一次有界采集，优先投递当前批次并在预算内排空积压。"""

    def __init__(
        self,
        store: Store,
        source: Source,
        *,
        notifier: Notifier | None,
        settings: RunnerSettings,
        keyword_filter: KeywordFilter | None = None,
        clock: Clock | None = None,
        commit: bool = True,
        send: bool = True,
        allow_empty_baseline: bool = False,
    ) -> None:
        self.store = store
        self.source = source
        self.notifier = notifier
        self.settings = settings
        self.keyword_filter = keyword_filter or KeywordFilter()
        self.clock = clock or SystemClock()
        self.commit = commit
        self.send = send
        self.allow_empty_baseline = allow_empty_baseline
        self.last_fetch: FetchSummary | None = None
        self.last_digest: Digest | None = None
        self.last_digests: tuple[Digest, ...] = ()

    def run_once(self) -> RunSummary:
        run_id = uuid.uuid4().hex
        started_mono = self.clock.monotonic()
        started_at = self.clock.now()
        budget = _Budget(
            started_mono=started_mono,
            deadline_mono=started_mono + self.settings.run_timeout_seconds,
        )
        recovered = self.store.recover_sending(started_at)
        if recovered:
            # 进程崩溃留下的 sending 已被标成 unknown；本轮不会自动重发它们。
            self.store.set_state("last_recovered_sending_count", str(recovered))
        if self.commit:
            self._cleanup_if_due(started_at)

        paused = self.store.get_state("source_paused") == "1"
        next_poll = _parse_optional_iso(self.store.get_state("next_poll_at"))
        cooldown = _parse_optional_iso(self.store.get_state("cooldown_until"))
        if paused:
            result = self._finish_summary(
                run_id,
                started_mono,
                skipped=True,
                error_kind=ErrorKind.AUTH,
                error_message=self.store.get_state("pause_reason", "来源已暂停"),
            )
            return self._attach_send(result, started_mono, budget)
        if self.settings.interval_seconds > 0 and next_poll and started_at < next_poll:
            result = self._finish_summary(run_id, started_mono, skipped=True)
            return self._attach_send(result, started_mono, budget)
        cooldown_reason = self.store.get_state("cooldown_reason")
        if cooldown and cooldown_reason != "树洞返回 429":
            # 旧版本会为连续 incomplete 设置来源冷却；该状态不是远端限流，
            # 不能阻塞当前每 10 分钟的调度。清掉后续运行留下的旧保护状态。
            self.store.set_states({"cooldown_until": None, "cooldown_reason": None})
            cooldown = None
        if cooldown and started_at < cooldown:
            result = self._finish_summary(
                run_id,
                started_mono,
                skipped=True,
                error_kind=ErrorKind.RATE_LIMIT,
                error_message="来源仍在冷却期",
            )
            return self._attach_send(result, started_mono, budget)

        # 默认配置会持久化下次采集时间，避免调度器或手动重启形成高频循环。
        # interval_seconds=0 是显式关闭这一层应用冷却；请求间隔和整轮运行看门狗
        # 仍然生效，外部调度器的触发频率也不由此改变。
        next_poll_at = (
            started_at + timedelta(seconds=self.settings.interval_seconds)
            if self.settings.interval_seconds > 0
            else None
        )
        self.store.set_states(
            {
                "last_poll_started_at": _iso(started_at),
                "next_poll_at": _iso(next_poll_at) if next_poll_at else None,
            }
        )
        fetch = self._collect(budget)
        collection_safe = not self._budget_expired(budget)
        if not collection_safe:
            fetch = self._budget_exceeded_fetch(fetch)
        matched_ids: set[str] = set()
        if collection_safe:
            matched_ids = {
                post.id
                for post in fetch.candidate_posts
                if not post.is_pinned and self.keyword_filter.matches(post.text)
            }

        notification_parts: tuple[Digest, ...] = ()
        notification_error: SourceError | None = None
        if collection_safe and not fetch.baseline:
            try:
                notification_parts = self._notification_for(
                    fetch,
                    matched_ids=matched_ids,
                    created_at=started_at,
                    budget=budget,
                )
            except SourceError as exc:
                # 采集结果仍可安全持久化为累计待通知；下一轮会重新尝试获取完整回复。
                notification_error = exc
            except (TypeError, ValueError):
                notification_error = SourceError(
                    ErrorKind.CONTRACT,
                    "通知内容无法安全构造，帖子已保留为累计待通知",
                )

        if collection_safe:
            fetch = replace(fetch, request_count=budget.request_count)
        self.last_fetch = fetch
        self.last_digests = notification_parts
        self.last_digest = notification_parts[0] if notification_parts else None

        try:
            if (
                self.commit
                and collection_safe
                and (fetch.baseline or fetch.candidate_posts or fetch.coverage)
            ):
                self.store.commit_collection(
                    now=self.clock.now(),
                    posts=fetch.posts if fetch.baseline else fetch.candidate_posts,
                    matched_ids=matched_ids,
                    digests=notification_parts,
                    coverage=fetch.coverage,
                    proposed_watermark=fetch.proposed_watermark,
                    baseline=fetch.baseline,
                )
        except StoreError:
            # commit_collection 自身拥有事务边界；异常时不能写入“成功水位”。
            self._record_failure(
                kind=ErrorKind.UNKNOWN,
                message="本地数据库事务失败，已保留旧水位",
                now=self.clock.now(),
                temporary=False,
            )
            return self._finish_summary(
                run_id,
                started_mono,
                pages=fetch.pages,
                request_count=fetch.request_count,
                new_count=len(fetch.candidate_posts),
                matched_count=len(matched_ids),
                coverage=fetch.coverage,
                batch_id=None,
                error_kind=ErrorKind.UNKNOWN,
                error_message="本地数据库事务失败，已保留旧水位",
            )

        self._record_fetch(fetch, self.clock.now())
        if notification_error is not None and self.commit:
            self.store.set_states(
                {
                    "last_notification_error_kind": notification_error.kind.value,
                    "last_notification_error_message": notification_error.message,
                    "last_notification_error_at": _iso(self.clock.now()),
                }
            )
        elif notification_parts and self.commit:
            self.store.set_states(
                {
                    "last_notification_error_kind": None,
                    "last_notification_error_message": None,
                    "last_notification_error_at": None,
                }
            )
        result = self._finish_summary(
            run_id,
            started_mono,
            pages=fetch.pages,
            request_count=fetch.request_count,
            new_count=len(fetch.candidate_posts),
            matched_count=len(matched_ids),
            coverage=fetch.coverage,
            batch_id=notification_parts[0].batch_id if notification_parts else None,
            error_kind=fetch.error_kind
            or (notification_error.kind if notification_error else None),
            error_message=fetch.error_message
            or (notification_error.message if notification_error else None),
        )
        result = self._attach_send(result, started_mono, budget)
        return result

    def fetch_latest_page(self) -> tuple[Page, int]:
        """按同一轮 HTTP 预算和重试规则只获取最新页，供基线重设和只读探测使用。"""

        started = self.clock.monotonic()
        budget = _Budget(
            started_mono=started,
            deadline_mono=started + self.settings.run_timeout_seconds,
        )
        page = self._fetch_page(None, budget)
        return page, budget.request_count

    def record_source_error(self, error: SourceError, now: datetime | None = None) -> None:
        """记录不进入常规采集编排的单页操作错误（例如基线重设）。"""

        current = now or self.clock.now()
        self._record_fetch(
            FetchSummary(
                posts=(),
                candidate_posts=(),
                coverage=Coverage.INCOMPLETE,
                request_count=0,
                pages=0,
                proposed_watermark=None,
                error_kind=error.kind,
                error_message=error.message,
                retry_after_seconds=error.retry_after_seconds,
            ),
            current,
        )

    def _collect(self, budget: _Budget) -> FetchSummary:
        initialized, watermark = self.store.baseline()
        old_watermark: int | None
        if watermark:
            try:
                old_watermark = int(watermark)
            except ValueError:
                return FetchSummary(
                    posts=(),
                    candidate_posts=(),
                    coverage=Coverage.INCOMPLETE,
                    request_count=0,
                    pages=0,
                    proposed_watermark=None,
                    error_kind=ErrorKind.CONTRACT,
                    error_message="本地水位不是十进制 ID，已停止采集",
                    baseline=False,
                )
        else:
            old_watermark = None

        if not initialized:
            try:
                page = self._fetch_page(None, budget)
            except SourceError as exc:
                return self._failed_fetch(budget, exc)
            budget.pages = 1
            if page.next_page is not None and not isinstance(page.next_page, str):
                return FetchSummary(
                    posts=(),
                    candidate_posts=(),
                    coverage=Coverage.INCOMPLETE,
                    request_count=budget.request_count,
                    pages=budget.pages,
                    proposed_watermark=None,
                    error_kind=ErrorKind.CONTRACT,
                    error_message="树洞分页 token 不是字符串，未建立基线",
                    baseline=False,
                )
            if page.exhausted and page.next_page is not None:
                return FetchSummary(
                    posts=(),
                    candidate_posts=(),
                    coverage=Coverage.INCOMPLETE,
                    request_count=budget.request_count,
                    pages=budget.pages,
                    proposed_watermark=None,
                    error_kind=ErrorKind.CONTRACT,
                    error_message="树洞分页同时声明 exhausted 和 next_page，未建立基线",
                    baseline=False,
                )
            ordinary = [post for post in _unique_posts(page.posts) if not post.is_pinned]
            if not ordinary and not self.allow_empty_baseline:
                return FetchSummary(
                    posts=tuple(_unique_posts(page.posts)),
                    candidate_posts=(),
                    coverage=Coverage.INCOMPLETE,
                    request_count=budget.request_count,
                    pages=budget.pages,
                    proposed_watermark=None,
                    error_kind=ErrorKind.CONTRACT,
                    error_message="首个列表页没有可确认的普通帖，未建立基线",
                    baseline=False,
                )
            proposed = _max_id(ordinary) if ordinary else ""
            return FetchSummary(
                posts=tuple(_unique_posts(page.posts)),
                candidate_posts=(),
                coverage=Coverage.BASELINE,
                request_count=budget.request_count,
                pages=budget.pages,
                proposed_watermark=proposed,
                baseline=True,
            )

        candidates: list[Post] = []
        seen_candidates: set[str] = set()
        seen_page_signatures: set[tuple[str, ...]] = set()
        seen_tokens: set[str | None] = set()
        ordinary_seen: list[Post] = []
        token: str | None = None
        boundary_reached = False
        incomplete_error: SourceError | None = None
        post_limit_reached = False

        # max_pages=0 表示不按页数截断；max_posts=0 表示不按候选数量截断。
        # 两者都受分页 token/重复页检测和整轮运行看门狗保护，避免来源契约
        # 异常时退化成无休止请求。
        page_indexes = count() if self.settings.max_pages == 0 else range(self.settings.max_pages)
        for _page_index in page_indexes:
            if token in seen_tokens:
                incomplete_error = SourceError(
                    ErrorKind.CONTRACT, "树洞分页 token 重复，未推进水位"
                )
                break
            seen_tokens.add(token)
            try:
                page = self._fetch_page(token, budget)
            except SourceError as exc:
                incomplete_error = exc
                break
            budget.pages += 1
            if page.next_page is not None and not isinstance(page.next_page, str):
                incomplete_error = SourceError(ErrorKind.CONTRACT, "树洞分页 token 不是字符串")
                break
            if page.exhausted and page.next_page is not None:
                incomplete_error = SourceError(
                    ErrorKind.CONTRACT, "树洞分页同时声明 exhausted 和 next_page"
                )
                break
            page_posts = _unique_posts(page.posts)
            signature = tuple(post.id for post in page_posts)
            if signature in seen_page_signatures:
                incomplete_error = SourceError(ErrorKind.CONTRACT, "树洞返回重复页，未推进水位")
                break
            seen_page_signatures.add(signature)
            known_ids = self.store.known_post_ids([post.id for post in page_posts])

            for post in page_posts:
                if post.is_pinned:
                    is_new_region = old_watermark is None or int(post.id) > old_watermark
                else:
                    ordinary_seen.append(post)
                    is_new_region = old_watermark is None or int(post.id) > old_watermark
                    if old_watermark is not None and int(post.id) <= old_watermark:
                        boundary_reached = True
                if not is_new_region or post.id in known_ids or post.id in seen_candidates:
                    continue
                candidates.append(post)
                seen_candidates.add(post.id)
                if self.settings.max_posts > 0 and len(candidates) >= self.settings.max_posts:
                    post_limit_reached = True
                    break

            if boundary_reached:
                break
            if post_limit_reached:
                incomplete_error = SourceError(
                    ErrorKind.TEMPORARY, "达到单轮新帖数量预算，未到达旧边界"
                )
                break
            if page.exhausted:
                break
            if page.next_page is None:
                break
            if page.next_page in seen_tokens:
                incomplete_error = SourceError(ErrorKind.CONTRACT, "树洞分页循环，未推进水位")
                break
            token = page.next_page
        else:
            incomplete_error = SourceError(ErrorKind.TEMPORARY, "达到单轮最大页数，未到达旧边界")

        bounded = incomplete_error is None and (boundary_reached or page.exhausted)
        # 只有边界或来源显式声明 exhausted 才算 bounded；next_page 缺失本身不算结束。
        if incomplete_error is None:
            # 循环自然结束时，只有 boundary 或最后一个响应显式 exhausted 才 bounded。
            if not bounded and page.next_page is None:
                incomplete_error = SourceError(ErrorKind.TEMPORARY, "列表未声明结束，未推进水位")

        coverage = Coverage.BOUNDED if bounded and incomplete_error is None else Coverage.INCOMPLETE
        proposed = _max_id(ordinary_seen) if ordinary_seen else (watermark if bounded else None)
        if old_watermark is None and bounded and not ordinary_seen:
            proposed = ""
        return FetchSummary(
            posts=tuple(candidates),
            candidate_posts=tuple(candidates),
            coverage=coverage,
            request_count=budget.request_count,
            pages=budget.pages,
            proposed_watermark=proposed if coverage == Coverage.BOUNDED else None,
            error_kind=incomplete_error.kind if incomplete_error else None,
            error_message=incomplete_error.message if incomplete_error else None,
            retry_after_seconds=incomplete_error.retry_after_seconds if incomplete_error else None,
            baseline=False,
        )

    def _fetch_page(self, token: str | None, budget: _Budget) -> Page:
        last_error: SourceError | None = None
        for attempt in range(2):
            if self._request_budget_exhausted(budget):
                raise SourceError(ErrorKind.TEMPORARY, "达到单轮 HTTP 请求预算")
            self._wait_between_requests(budget)
            if self.clock.monotonic() >= budget.deadline_mono:
                raise SourceError(ErrorKind.TEMPORARY, "达到单轮运行时间上限")
            budget.request_count += 1
            budget.last_request_mono = self.clock.monotonic()
            try:
                remaining = budget.remaining(self.clock.monotonic())
                bounded_fetch = getattr(self.source, "fetch_page_with_timeout", None)
                if callable(bounded_fetch):
                    page = bounded_fetch(
                        token,
                        self.settings.page_size,
                        timeout_seconds=remaining,
                    )
                else:
                    page = self.source.fetch_page(token, self.settings.page_size)
                if self._budget_expired(budget):
                    raise SourceError(ErrorKind.TEMPORARY, "来源响应超过单轮运行时间上限")
                return page
            except SourceError as exc:
                last_error = exc
                if self._budget_expired(budget):
                    raise SourceError(
                        ErrorKind.TEMPORARY, "来源请求期间达到单轮运行时间上限"
                    ) from exc
                retryable = exc.kind in {ErrorKind.TEMPORARY, ErrorKind.TEMPORARY_NOT_SENT}
                if not retryable or attempt == 1 or self._request_budget_exhausted(budget):
                    raise
                wait = max(self.settings.retry_wait_seconds, self.settings.request_spacing_seconds)
                self._sleep_with_deadline(wait, budget)
            except Exception as exc:
                last_error = SourceError(ErrorKind.TEMPORARY, "来源请求异常，未记录原始异常")
                if self._budget_expired(budget):
                    raise SourceError(
                        ErrorKind.TEMPORARY, "来源请求期间达到单轮运行时间上限"
                    ) from exc
                if attempt == 1 or self._request_budget_exhausted(budget):
                    raise last_error from exc
                wait = max(self.settings.retry_wait_seconds, self.settings.request_spacing_seconds)
                self._sleep_with_deadline(wait, budget)
        raise last_error or SourceError(ErrorKind.TEMPORARY, "来源请求失败")

    def _request_budget_exhausted(self, budget: _Budget) -> bool:
        """0 表示生产配置不限制单轮请求次数。"""
        return self.settings.max_requests > 0 and budget.request_count >= self.settings.max_requests

    def _wait_between_requests(self, budget: _Budget) -> None:
        if budget.last_request_mono is None:
            return
        elapsed = self.clock.monotonic() - budget.last_request_mono
        wait = self.settings.request_spacing_seconds - elapsed
        if wait > 0:
            self._sleep_with_deadline(wait, budget)

    def _sleep_with_deadline(self, seconds: float, budget: _Budget) -> None:
        if seconds <= 0:
            return
        if self.clock.monotonic() + seconds > budget.deadline_mono:
            raise SourceError(ErrorKind.TEMPORARY, "等待重试时达到单轮运行时间上限")
        self.clock.sleep(seconds)

    def _budget_expired(self, budget: _Budget) -> bool:
        return budget.remaining(self.clock.monotonic()) <= 0

    def _budget_exceeded_fetch(self, fetch: FetchSummary) -> FetchSummary:
        return FetchSummary(
            posts=(),
            candidate_posts=(),
            coverage=Coverage.INCOMPLETE,
            request_count=fetch.request_count,
            pages=fetch.pages,
            proposed_watermark=None,
            error_kind=ErrorKind.TEMPORARY,
            error_message="达到单轮运行时间上限，未提交本轮采集结果",
            baseline=False,
        )

    def _notification_for(
        self,
        fetch: FetchSummary,
        *,
        matched_ids: set[str],
        created_at: datetime,
        budget: _Budget,
    ) -> tuple[Digest, ...]:
        current = [post for post in fetch.candidate_posts if post.id in matched_ids]
        accumulated = _unique_posts([*self.store.pending_notification_posts(), *current])
        if not accumulated:
            return ()

        options = self.settings.digest_options
        if options is None:
            options = DigestOptions(timezone=self.settings.timezone)

        focused: list[Post] = []
        if options.attention is not None and options.attention.enabled:
            focused = [
                post
                for post, match in rank_posts(
                    accumulated,
                    options.attention,
                    excerpt_chars=options.snippet_chars,
                )
                if match.relevant
            ]
        threshold_hit = len(accumulated) > self.settings.push_when_post_count_exceeds
        if not threshold_hit and not focused:
            return ()

        comments_by_post: dict[str, tuple[Comment, ...]] = {}
        if options.content_mode != "links_only":
            for post in focused:
                comments_by_post[post.id] = self._fetch_all_comments(post.id, budget)
        return build_notification_digests(
            accumulated,
            comments_by_post=comments_by_post,
            coverage=fetch.coverage,
            created_at=created_at,
            options=options,
        )

    def _fetch_all_comments(self, post_id: str, budget: _Budget) -> tuple[Comment, ...]:
        comments: list[Comment] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str | None] = set()
        token: str | None = None
        declared_total: int | None = None
        while True:
            if token in seen_tokens:
                raise SourceError(ErrorKind.CONTRACT, "树洞回复分页 token 重复")
            seen_tokens.add(token)
            page = self._fetch_comment_page(post_id, token, budget)
            if page.next_page is not None and not isinstance(page.next_page, str):
                raise SourceError(ErrorKind.CONTRACT, "树洞回复分页 token 不是字符串")
            if page.exhausted and page.next_page is not None:
                raise SourceError(ErrorKind.CONTRACT, "树洞回复分页状态互相矛盾")
            if page.total < 0:
                raise SourceError(ErrorKind.CONTRACT, "树洞回复总数不能为负数")
            if declared_total is None:
                declared_total = page.total
            elif page.total != declared_total:
                raise SourceError(
                    ErrorKind.TEMPORARY,
                    "树洞回复在分页期间发生变化，已延后通知以保证内容完整",
                )
            for comment in page.comments:
                if comment.post_id != post_id:
                    raise SourceError(ErrorKind.CONTRACT, "树洞回复所属帖子与请求不一致")
                if comment.id in seen_ids:
                    raise SourceError(ErrorKind.CONTRACT, "树洞回复分页包含重复回复")
                seen_ids.add(comment.id)
                comments.append(comment)
            if page.exhausted:
                if len(comments) != declared_total:
                    raise SourceError(
                        ErrorKind.TEMPORARY,
                        "树洞回复在分页期间发生变化，已延后通知以保证内容完整",
                    )
                return tuple(comments)
            if page.next_page is None or page.next_page in seen_tokens:
                raise SourceError(ErrorKind.CONTRACT, "树洞回复分页未提供有效下一页")
            token = page.next_page

    def _fetch_comment_page(
        self,
        post_id: str,
        token: str | None,
        budget: _Budget,
    ) -> CommentPage:
        last_error: SourceError | None = None
        for attempt in range(2):
            if self._request_budget_exhausted(budget):
                raise SourceError(ErrorKind.TEMPORARY, "达到单轮 HTTP 请求预算")
            self._wait_between_requests(budget)
            if self.clock.monotonic() >= budget.deadline_mono:
                raise SourceError(ErrorKind.TEMPORARY, "达到单轮运行时间上限")
            budget.request_count += 1
            budget.last_request_mono = self.clock.monotonic()
            try:
                remaining = budget.remaining(self.clock.monotonic())
                bounded_fetch = getattr(self.source, "fetch_comments_with_timeout", None)
                if callable(bounded_fetch):
                    page = bounded_fetch(
                        post_id,
                        token,
                        self.settings.comment_page_size,
                        timeout_seconds=remaining,
                    )
                else:
                    page = self.source.fetch_comments(
                        post_id,
                        token,
                        self.settings.comment_page_size,
                    )
                if self._budget_expired(budget):
                    raise SourceError(ErrorKind.TEMPORARY, "回复响应超过单轮运行时间上限")
                return page
            except SourceError as exc:
                last_error = exc
                retryable = exc.kind in {ErrorKind.TEMPORARY, ErrorKind.TEMPORARY_NOT_SENT}
                if self._budget_expired(budget) or not retryable or attempt == 1:
                    raise
                wait = max(self.settings.retry_wait_seconds, self.settings.request_spacing_seconds)
                self._sleep_with_deadline(wait, budget)
            except Exception as exc:
                last_error = SourceError(ErrorKind.TEMPORARY, "树洞回复请求异常")
                if self._budget_expired(budget) or attempt == 1:
                    raise last_error from exc
                wait = max(self.settings.retry_wait_seconds, self.settings.request_spacing_seconds)
                self._sleep_with_deadline(wait, budget)
        raise last_error or SourceError(ErrorKind.TEMPORARY, "树洞回复请求失败")

    def _record_fetch(self, fetch: FetchSummary, now: datetime) -> None:
        values: dict[str, str | None] = {"last_poll_finished_at": _iso(now)}
        if self.commit:
            values.update(
                {
                    "coverage": fetch.coverage.value,
                    "last_error_kind": fetch.error_kind.value if fetch.error_kind else None,
                    "last_error_message": fetch.error_message,
                }
            )
        elif fetch.error_kind is not None:
            values.update(
                {
                    "last_error_kind": fetch.error_kind.value,
                    "last_error_message": fetch.error_message,
                }
            )
        else:
            values.update({"last_error_kind": None, "last_error_message": None})
        if fetch.error_kind is None and fetch.coverage in {Coverage.BASELINE, Coverage.BOUNDED}:
            values["last_success_at"] = _iso(now)
            values["consecutive_temp_failures"] = "0"
            values["cooldown_until"] = None
            values["cooldown_reason"] = None
        elif fetch.error_kind in {ErrorKind.TEMPORARY, ErrorKind.TEMPORARY_NOT_SENT}:
            failures = int(self.store.get_state("consecutive_temp_failures", "0") or "0") + 1
            values["consecutive_temp_failures"] = str(failures)
            # incomplete/超时可能来自分页漂移或本轮看门狗，不应阻塞下一轮。
            # 真实 HTTP 429 在下方单独持久化远端限流冷却。
        if fetch.error_kind == ErrorKind.RATE_LIMIT:
            retry_after = max(3600, fetch.retry_after_seconds or 0)
            values["cooldown_until"] = _iso(now + timedelta(seconds=retry_after))
            values["cooldown_reason"] = "树洞返回 429"
        if fetch.error_kind in {
            ErrorKind.AUTH,
            ErrorKind.ACCESS_DENIED,
            ErrorKind.BUSINESS,
            ErrorKind.CONTRACT,
        }:
            values["source_paused"] = "1"
            values["pause_reason"] = fetch.error_message or fetch.error_kind.value
        self.store.set_states(values)

    def _record_failure(
        self, *, kind: ErrorKind, message: str, now: datetime, temporary: bool
    ) -> None:
        values: dict[str, str | None] = {
            "last_poll_finished_at": _iso(now),
            "last_error_kind": kind.value,
            "last_error_message": message,
        }
        if temporary:
            failures = int(self.store.get_state("consecutive_temp_failures", "0") or "0") + 1
            values["consecutive_temp_failures"] = str(failures)
        self.store.set_states(values)

    def _failed_fetch(self, budget: _Budget, exc: SourceError) -> FetchSummary:
        return FetchSummary(
            posts=(),
            candidate_posts=(),
            coverage=Coverage.INCOMPLETE,
            request_count=budget.request_count,
            pages=budget.pages,
            proposed_watermark=None,
            error_kind=exc.kind,
            error_message=exc.message,
            retry_after_seconds=exc.retry_after_seconds,
            baseline=False,
        )

    def _attach_send(self, summary: RunSummary, started_mono: float, budget: _Budget) -> RunSummary:
        if not self.commit or not self.send or self.notifier is None:
            return replace(summary, elapsed_seconds=self._elapsed(started_mono))
        if self._budget_expired(budget):
            batch_state = (
                self.store.notification_group_status(summary.batch_id)
                if summary.batch_id
                else summary.batch_state
            )
            return replace(
                summary,
                batch_state=batch_state,
                error_kind=summary.error_kind or ErrorKind.TEMPORARY,
                error_message=summary.error_message or "剩余运行预算不足，未启动通知发送",
                elapsed_seconds=self._elapsed(started_mono),
            )
        outcomes: list[_SendOutcome] = []
        preferred_batch_id = summary.batch_id
        send_error_kind: ErrorKind | None = None
        send_error_message: str | None = None

        while not self._budget_expired(budget):
            outcome = self._send_one(
                self.clock.now(),
                budget,
                preferred_batch_id=preferred_batch_id,
            )
            if outcome.batch_id is None:
                send_error_kind = send_error_kind or outcome.error_kind
                send_error_message = send_error_message or outcome.error_message
                break
            outcomes.append(outcome)
            send_error_kind = send_error_kind or outcome.error_kind
            send_error_message = send_error_message or outcome.error_message
            if outcome.state not in {SendState.ACCEPTED, SendState.DELIVERED}:
                break
            if self.store.due_outbox_count(self.clock.now()) == 0:
                break
            spacing = self.settings.send_spacing_seconds
            if spacing > 0:
                if budget.remaining(self.clock.monotonic()) <= spacing:
                    send_error_kind = send_error_kind or ErrorKind.TEMPORARY
                    send_error_message = send_error_message or (
                        "剩余运行预算不足，积压通知将在下轮继续发送"
                    )
                    break
                self.clock.sleep(spacing)

        batch_state = (
            self.store.notification_group_status(summary.batch_id)
            if summary.batch_id
            else summary.batch_state
        )
        pending_count = self.store.outbox_counts().get(SendState.PENDING.value, 0)
        return replace(
            summary,
            batch_state=batch_state,
            send_state=outcomes[-1].state if outcomes else None,
            send_count=len(outcomes),
            sent_batch_ids=tuple(
                outcome.batch_id for outcome in outcomes if outcome.batch_id is not None
            ),
            pending_send_count=pending_count,
            error_kind=summary.error_kind or send_error_kind,
            error_message=summary.error_message or send_error_message,
            elapsed_seconds=self._elapsed(started_mono),
        )

    def _send_one(
        self,
        now: datetime,
        budget: _Budget,
        *,
        preferred_batch_id: str | None = None,
    ) -> _SendOutcome:
        if self._budget_expired(budget):
            return _SendOutcome(
                error_kind=ErrorKind.TEMPORARY,
                error_message="剩余运行预算不足，未启动通知发送",
            )
        day_start, day_end = _day_bounds(now, self.settings.timezone)
        decision: ClaimDecision = self.store.claim_outbox(
            now=now,
            daily_limit=self.settings.daily_send_limit,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=self.settings.pending_ttl_hours),
            max_attempts=self.settings.max_send_attempts,
            preferred_batch_id=preferred_batch_id,
        )
        claim = decision.claim
        if claim is None:
            if decision.reason == "notification_channel_cooldown":
                return _SendOutcome(
                    error_kind=ErrorKind.RATE_LIMIT,
                    error_message="通知渠道仍在冷却期",
                )
            if decision.reason == "daily_send_budget_exhausted":
                return _SendOutcome(
                    error_kind=ErrorKind.QUOTA,
                    error_message="今日通知尝试预算已用尽",
                )
            return _SendOutcome()
        row = self.store.outbox(claim.batch_id)
        if row is None:
            raise StoreError("已认领通知批次不存在")
        digest = Digest(
            batch_id=claim.batch_id,
            group_id=str(row["group_id"]),
            part_index=int(row["part_index"]),
            part_count=int(row["part_count"]),
            title=claim.title,
            content=claim.content,
            post_ids=tuple(str(row["id"]) for row in self.store.posts_for_batch(claim.batch_id)),
            created_at=claim.created_at,
            coverage=Coverage(self.store.get_state("coverage", Coverage.BOUNDED.value)),
            post_count=int(row["post_count"]),
            shown_count=int(row["shown_count"]),
        )
        if self._budget_expired(budget):
            result = SendResult(
                state=SendState.PENDING,
                error_kind=ErrorKind.TEMPORARY_NOT_SENT,
                error_message="剩余运行预算耗尽，未启动通知请求",
                retry_at=self.clock.now()
                + timedelta(seconds=self.settings.send_retry_spacing_seconds),
            )
            self.store.finish_send(claim.attempt_id, now=self.clock.now(), result=result)
            return _SendOutcome(
                batch_id=claim.batch_id,
                state=result.state,
                error_kind=result.error_kind,
                error_message=result.error_message,
            )
        try:
            bounded_send = getattr(self.notifier, "send_with_timeout", None)
            if callable(bounded_send):
                result = bounded_send(
                    digest,
                    timeout_seconds=budget.remaining(self.clock.monotonic()),
                )
            else:
                result = self.notifier.send(digest)
        except Exception:
            result = SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.UNKNOWN,
                error_message="通知请求结果未知，已暂停自动重发",
            )
        if self._budget_expired(budget) and not (
            result.state == SendState.PENDING and result.error_kind == ErrorKind.TEMPORARY_NOT_SENT
        ):
            result = SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.UNKNOWN,
                error_message="通知调用超过单轮运行时间上限，结果未知，已暂停自动重发",
            )
        if result.state == SendState.PENDING:
            minimum_retry_at = now + timedelta(seconds=self.settings.send_retry_spacing_seconds)
            retry_at = result.retry_at or minimum_retry_at
            if retry_at < minimum_retry_at:
                retry_at = minimum_retry_at
            result = SendResult(
                state=result.state,
                provider_receipt=result.provider_receipt,
                error_kind=result.error_kind,
                error_message=result.error_message,
                retry_at=retry_at,
            )
        self.store.finish_send(claim.attempt_id, now=self.clock.now(), result=result)
        return _SendOutcome(
            batch_id=claim.batch_id,
            state=result.state,
            error_kind=result.error_kind,
            error_message=result.error_message,
        )

    def _cleanup_if_due(self, now: datetime) -> None:
        last_cleanup = _parse_optional_iso(self.store.get_state("last_cleanup_at"))
        pending = self.store.get_state("cleanup_pending") == "1"
        if (
            not pending
            and last_cleanup is not None
            and now < last_cleanup + timedelta(seconds=self.settings.cleanup_interval_seconds)
        ):
            return
        cleanup = self.store.cleanup(
            now=now,
            post_retention=timedelta(days=7),
            audit_retention=timedelta(days=30),
            limit=self.settings.cleanup_batch_size,
        )
        self.store.set_states(
            {
                "last_cleanup_at": _iso(now),
                "cleanup_pending": "1" if cleanup.has_more else None,
                "last_cleanup_summary": (
                    f"content={cleanup.content_cleared},snippets={cleanup.snippets_cleared},"
                    f"attempts={cleanup.attempts_deleted},posts={cleanup.posts_deleted},"
                    f"outbox={cleanup.outbox_deleted}"
                ),
            }
        )

    def _elapsed(self, started_mono: float) -> float:
        return max(0.0, self.clock.monotonic() - started_mono)

    def _finish_summary(
        self,
        run_id: str,
        started_mono: float,
        *,
        pages: int = 0,
        request_count: int = 0,
        new_count: int = 0,
        matched_count: int = 0,
        coverage: Coverage | None = None,
        batch_id: str | None = None,
        skipped: bool = False,
        error_kind: ErrorKind | None = None,
        error_message: str | None = None,
    ) -> RunSummary:
        return RunSummary(
            run_id=run_id,
            pages=pages,
            request_count=request_count,
            new_count=new_count,
            matched_count=matched_count,
            coverage=coverage,
            batch_id=batch_id,
            skipped=skipped,
            error_kind=error_kind,
            error_message=error_message,
            elapsed_seconds=max(0.0, self.clock.monotonic() - started_mono),
        )


def _unique_posts(posts: list[Post]) -> list[Post]:
    result: list[Post] = []
    seen: set[str] = set()
    for post in posts:
        if post.id not in seen:
            result.append(post)
            seen.add(post.id)
    return result


def _max_id(posts: list[Post]) -> str | None:
    if not posts:
        return None
    return max(posts, key=lambda post: int(post.id)).id


def _parse_optional_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _day_bounds(now: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    local = now.astimezone(timezone)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
