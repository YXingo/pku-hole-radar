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

from .digest import DigestOptions, build_digest
from .filtering import KeywordFilter
from .models import Coverage, ErrorKind, FetchSummary, Page, Post, RunSummary, SendResult, SendState
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
    send_retry_spacing_seconds: float = 1800
    cleanup_interval_seconds: float = 86400
    cleanup_batch_size: int = 500
    digest_options: DigestOptions | None = None


@dataclass(slots=True)
class _Budget:
    started_mono: float
    deadline_mono: float
    request_count: int = 0
    pages: int = 0
    last_request_mono: float | None = None

    def remaining(self, current_mono: float) -> float:
        return self.deadline_mono - current_mono


class Runner:
    """执行一次有界采集和最多一次投递；网络请求不在数据库事务内。"""

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
        self.last_digest = None

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
        if self._budget_expired(budget):
            fetch = self._budget_exceeded_fetch(fetch)
        digest_error = False
        digest = None
        if not self._budget_expired(budget):
            try:
                digest = self._digest_for(fetch, started_at)
            except (TypeError, ValueError):
                # 链接模板或长度配置异常时，宁可不入库也不把候选标成已处理；否则
                # 修复配置后会因去重记录而永久跳过这批帖子。
                fetch = FetchSummary(
                    posts=fetch.posts,
                    candidate_posts=fetch.candidate_posts,
                    coverage=Coverage.INCOMPLETE,
                    request_count=fetch.request_count,
                    pages=fetch.pages,
                    proposed_watermark=None,
                    error_kind=ErrorKind.CONTRACT,
                    error_message="简报无法安全构造，未推进水位",
                    baseline=False,
                )
                digest = None
                digest_error = True
        self.last_fetch = fetch
        self.last_digest = digest

        matched_ids: set[str] = set()
        if not self._budget_expired(budget):
            matched_ids = {
                post.id
                for post in fetch.candidate_posts
                if not post.is_pinned and self.keyword_filter.matches(post.text)
            }
        if self._budget_expired(budget):
            fetch = self._budget_exceeded_fetch(fetch)
            digest = None
            digest_error = True

        try:
            if (
                self.commit
                and not digest_error
                and not self._budget_expired(budget)
                and (fetch.baseline or fetch.candidate_posts or fetch.coverage)
            ):
                self.store.commit_collection(
                    now=self.clock.now(),
                    posts=fetch.posts if fetch.baseline else fetch.candidate_posts,
                    matched_ids=matched_ids,
                    digest=digest,
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
        result = self._finish_summary(
            run_id,
            started_mono,
            pages=fetch.pages,
            request_count=fetch.request_count,
            new_count=len(fetch.candidate_posts),
            matched_count=len(matched_ids),
            coverage=fetch.coverage,
            batch_id=digest.batch_id if digest else None,
            error_kind=fetch.error_kind,
            error_message=fetch.error_message,
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

    def _digest_for(self, fetch: FetchSummary, created_at: datetime):
        if fetch.baseline or not fetch.candidate_posts:
            return None
        matching = [
            post
            for post in fetch.candidate_posts
            if not post.is_pinned and self.keyword_filter.matches(post.text)
        ]
        if not matching:
            return None
        options = self.settings.digest_options
        if options is None:
            options = DigestOptions(timezone=self.settings.timezone)
        return build_digest(
            matching,
            coverage=fetch.coverage,
            created_at=created_at,
            options=options,
        )

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
                self.store.latest_outbox_status(summary.batch_id)
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
        now = self.clock.now()
        send_state, batch_state, send_error_kind, send_error_message = self._send_one(now, budget)
        if send_state is None:
            if summary.batch_id and batch_state is None:
                batch_state = self.store.latest_outbox_status(summary.batch_id)
            return replace(
                summary,
                batch_state=batch_state or summary.batch_state,
                error_kind=summary.error_kind or send_error_kind,
                error_message=summary.error_message or send_error_message,
                elapsed_seconds=self._elapsed(started_mono),
            )
        error_kind = summary.error_kind or send_error_kind
        error_message = summary.error_message or send_error_message
        return RunSummary(
            run_id=summary.run_id,
            pages=summary.pages,
            request_count=summary.request_count,
            new_count=summary.new_count,
            matched_count=summary.matched_count,
            coverage=summary.coverage,
            batch_id=summary.batch_id,
            batch_state=batch_state,
            send_state=send_state,
            skipped=summary.skipped,
            error_kind=error_kind,
            error_message=error_message,
            elapsed_seconds=self._elapsed(started_mono),
        )

    def _send_one(
        self, now: datetime, budget: _Budget
    ) -> tuple[SendState | None, SendState | None, ErrorKind | None, str | None]:
        if self._budget_expired(budget):
            return None, None, ErrorKind.TEMPORARY, "剩余运行预算不足，未启动通知发送"
        day_start, day_end = _day_bounds(now, self.settings.timezone)
        decision: ClaimDecision = self.store.claim_outbox(
            now=now,
            daily_limit=self.settings.daily_send_limit,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=self.settings.pending_ttl_hours),
            max_attempts=self.settings.max_send_attempts,
        )
        claim = decision.claim
        if claim is None:
            if decision.reason == "notification_channel_cooldown":
                return None, None, ErrorKind.RATE_LIMIT, "通知渠道仍在冷却期"
            if decision.reason == "daily_send_budget_exhausted":
                return None, None, ErrorKind.QUOTA, "今日通知尝试预算已用尽"
            return None, None, None, None
        from .models import Digest

        digest = Digest(
            batch_id=claim.batch_id,
            title=claim.title,
            content=claim.content,
            post_ids=tuple(str(row["id"]) for row in self.store.posts_for_batch(claim.batch_id)),
            created_at=claim.created_at,
            coverage=Coverage(self.store.get_state("coverage", Coverage.BOUNDED.value)),
            post_count=self.store.outbox(claim.batch_id)["post_count"],
            shown_count=self.store.outbox(claim.batch_id)["shown_count"],
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
            return result.state, result.state, result.error_kind, result.error_message
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
        return result.state, result.state, result.error_kind, result.error_message

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
