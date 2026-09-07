from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pku_hole_radar.digest import DigestOptions
from pku_hole_radar.models import Coverage, Digest, ErrorKind, Page, Post, SendResult, SendState
from pku_hole_radar.runner import LockBusy, ProcessLock, Runner, RunnerSettings
from pku_hole_radar.source import SequenceSource, SourceError
from pku_hole_radar.store import Store, StoreError


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(UTC)
        self.mono = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds
        self.value += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.value += timedelta(seconds=seconds)


class RecordingNotifier:
    def __init__(self, result: SendResult | None = None) -> None:
        self.result = result or SendResult(SendState.ACCEPTED, provider_receipt="fixture-receipt")
        self.digests: list[Digest] = []

    def send(self, digest: Digest) -> SendResult:
        self.digests.append(digest)
        return self.result


class AdvancingNotifier(RecordingNotifier):
    def __init__(self, clock: FakeClock, seconds: float, result: SendResult) -> None:
        super().__init__(result)
        self.clock = clock
        self.seconds = seconds

    def send(self, digest: Digest) -> SendResult:
        self.digests.append(digest)
        self.clock.advance(self.seconds)
        return self.result


class AdvancingSource:
    def __init__(self, clock: FakeClock, seconds: float, page: Page) -> None:
        self.clock = clock
        self.seconds = seconds
        self.page = page
        self.calls: list[tuple[str | None, int]] = []

    def fetch_page(self, page_token: str | None, page_size: int) -> Page:
        self.calls.append((page_token, page_size))
        self.clock.advance(self.seconds)
        return self.page


class AdvancingCommitStore(Store):
    def __init__(self, clock: FakeClock, seconds: float) -> None:
        super().__init__(":memory:")
        self.clock = clock
        self.seconds = seconds

    def commit_collection(self, **kwargs):
        result = super().commit_collection(**kwargs)
        self.clock.advance(self.seconds)
        return result


def make_post(post_id: int, text: str = "fixture", *, pinned: bool = False) -> Post:
    return Post(
        id=str(post_id),
        created_at=datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=post_id),
        text=text,
        url=f"https://fixture.test/post/{post_id}",
        is_pinned=pinned,
    )


def make_settings(**overrides: object) -> RunnerSettings:
    values: dict[str, object] = {
        "timezone": ZoneInfo("Asia/Shanghai"),
        "interval_seconds": 1800,
        "page_size": 30,
        "max_pages": 3,
        "max_requests": 6,
        "request_spacing_seconds": 2,
        "retry_wait_seconds": 5,
        "run_timeout_seconds": 180,
        "daily_send_limit": 60,
        "pending_ttl_hours": 24,
        "send_retry_spacing_seconds": 1800,
        "digest_options": DigestOptions(
            timezone=ZoneInfo("Asia/Shanghai"),
            allowed_hosts=frozenset({"fixture.test"}),
        ),
    }
    values.update(overrides)
    return RunnerSettings(**values)  # type: ignore[arg-type]


def seed_watermark(store: Store, watermark: str = "100") -> None:
    store.set_states(
        {
            "baseline_initialized": "1",
            "baseline_id": watermark,
            "watermark_id": watermark,
            "coverage": Coverage.BASELINE.value,
        }
    )


def seed_pending_batch(store: Store, post_id: int, batch_id: str, text: str = "hit") -> None:
    post = make_post(post_id, text)
    store.commit_collection(
        now=datetime(2026, 9, 5, tzinfo=UTC),
        posts=[post],
        matched_ids={post.id},
        digest=Digest(
            batch_id=batch_id,
            title=f"批次 {batch_id}",
            content="合成通知",
            post_ids=(post.id,),
            created_at=datetime(2026, 9, 5, tzinfo=UTC),
            coverage=Coverage.INCOMPLETE,
            post_count=1,
            shown_count=1,
        ),
        coverage=Coverage.INCOMPLETE,
        proposed_watermark=None,
    )


def run(
    store: Store,
    source: SequenceSource,
    clock: FakeClock,
    *,
    notifier: RecordingNotifier | None = None,
    keyword_filter=None,
    **settings: object,
):
    return Runner(
        store,
        source,
        notifier=notifier,
        settings=make_settings(**settings),
        keyword_filter=keyword_filter,
        clock=clock,
    ).run_once()


def test_baseline_then_restart_dedupe_and_single_batch(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
    database = tmp_path / "state.sqlite3"
    with Store(database) as store:
        first = run(
            store,
            SequenceSource(
                [Page([make_post(105), make_post(104), make_post(103)], exhausted=True)]
            ),
            clock,
        )
        assert first.coverage == Coverage.BASELINE
        assert first.matched_count == 0
        assert store.outbox_counts() == {}
        assert store.baseline() == (True, "105")

        clock.advance(1800)
        notifier = RecordingNotifier()
        second_source = SequenceSource(
            [Page([make_post(108, "new"), make_post(107), make_post(106, "new"), make_post(105)])]
        )
        second = run(store, second_source, clock, notifier=notifier)
        assert second.coverage == Coverage.BOUNDED
        assert second.new_count == 3
        assert len(notifier.digests) == 1
        assert notifier.digests[0].post_ids == ("108", "107", "106")
        assert store.baseline() == (True, "108")
        batch_id = notifier.digests[0].batch_id
        assert len(store.posts_for_batch(batch_id)) == 3

    # 重新打开数据库并重放同一页：唯一键和已见记录共同保证不重复。
    with Store(database) as restarted:
        clock.advance(1800)
        notifier = RecordingNotifier()
        third = run(
            restarted,
            SequenceSource(
                [
                    Page(
                        [
                            make_post(108, "edited"),
                            make_post(107),
                            make_post(106, "new"),
                            make_post(105),
                        ]
                    )
                ]
            ),
            clock,
            notifier=notifier,
        )
        assert third.new_count == 0
        assert third.batch_id is None
        assert notifier.digests == []
        assert restarted.outbox_counts() == {"accepted": 1}


def test_pinned_does_not_define_boundary_and_multiple_pages_are_scanned() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        source = SequenceSource(
            [
                Page(
                    [make_post(120, "hit"), make_post(200, "pinned", pinned=True), make_post(119)],
                    next_page="2",
                ),
                Page([make_post(180, "old pinned", pinned=True), make_post(100, "old")]),
            ]
        )
        notifier = RecordingNotifier()
        summary = run(store, source, FakeClock(datetime(2026, 9, 5, tzinfo=UTC)), notifier=notifier)
        assert summary.pages == 2
        assert source.calls == [(None, 30), ("2", 30)]
        assert summary.coverage == Coverage.BOUNDED
        assert store.baseline() == (True, "120")
        assert len(notifier.digests) == 1
        assert notifier.digests[0].post_ids == ("120", "119")
        assert store.known_post_ids(["200"]) == {"200"}
    finally:
        store.close()


def test_incomplete_page_keeps_watermark_and_seen_candidates_are_not_replayed() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        pages = [
            Page([make_post(110), make_post(109)], next_page="2"),
            Page([make_post(108), make_post(107)], next_page="3"),
            Page([make_post(106), make_post(105)], next_page="4"),
        ]
        first = run(store, SequenceSource(pages), clock, notifier=None)
        assert first.coverage == Coverage.INCOMPLETE
        assert store.baseline() == (True, "100")
        assert store.outbox_counts() == {"pending": 1}
        first_batch = store.list_outbox()[0]["batch_id"]
        assert len(store.posts_for_batch(first_batch)) == 6

        clock.advance(1800)
        second = run(
            store,
            SequenceSource(
                [
                    Page([make_post(110), make_post(109)], next_page="2"),
                    Page([make_post(108), make_post(107)], next_page="3"),
                    Page([make_post(100)], exhausted=True),
                ]
            ),
            clock,
            notifier=None,
        )
        assert second.coverage == Coverage.BOUNDED
        assert second.new_count == 0
        assert store.baseline() == (True, "110")
        assert store.outbox_counts() == {"pending": 1}
    finally:
        store.close()


def test_zero_max_pages_scans_until_boundary_within_request_budget() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource(
            [
                Page([make_post(110), make_post(109)], next_page="2"),
                Page([make_post(108), make_post(107)], next_page="3"),
                Page([make_post(106), make_post(105)], next_page="4"),
                Page([make_post(104), make_post(103)], next_page="5"),
                Page([make_post(100)], exhausted=True),
            ]
        )
        summary = run(
            store,
            source,
            clock,
            max_pages=0,
            max_requests=5,
            request_spacing_seconds=0,
        )
        assert summary.coverage == Coverage.BOUNDED
        assert summary.pages == 5
        assert summary.request_count == 5
        assert summary.new_count == 8
        assert source.calls == [(None, 30), ("2", 30), ("3", 30), ("4", 30), ("5", 30)]
    finally:
        store.close()


def test_zero_max_requests_scans_until_boundary_without_request_count_cap() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource(
            [
                Page([make_post(110)], next_page="2"),
                Page([make_post(109)], next_page="3"),
                Page([make_post(108)], next_page="4"),
                Page([make_post(107)], next_page="5"),
                Page([make_post(106)], next_page="6"),
                Page([make_post(105)], next_page="7"),
                Page([make_post(100)], exhausted=True),
            ]
        )
        summary = run(
            store,
            source,
            clock,
            interval_seconds=0,
            max_pages=0,
            max_requests=0,
            request_spacing_seconds=0,
        )

        assert summary.coverage == Coverage.BOUNDED
        assert summary.pages == 7
        assert summary.request_count == 7
        assert summary.new_count == 6
    finally:
        store.close()


def test_incomplete_rounds_do_not_trigger_source_cooldown() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))

        for _ in range(3):
            summary = run(
                store,
                SequenceSource([Page([make_post(110)], next_page="2")]),
                clock,
                interval_seconds=0,
                max_requests=1,
                request_spacing_seconds=0,
            )
            assert summary.coverage == Coverage.INCOMPLETE
            assert summary.error_kind == ErrorKind.TEMPORARY

        assert store.get_state("consecutive_temp_failures") == "3"
        assert store.get_state("cooldown_until") is None
        assert store.get_state("cooldown_reason") is None
    finally:
        store.close()


def test_legacy_incomplete_cooldown_is_cleared_and_does_not_skip_round() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        store.set_states(
            {
                "cooldown_until": (clock.now() + timedelta(hours=2)).isoformat(),
                "cooldown_reason": "连续三轮临时采集失败",
            }
        )
        source = SequenceSource([Page([make_post(101)], exhausted=True)])

        summary = run(store, source, clock, interval_seconds=0, request_spacing_seconds=0)

        assert summary.coverage == Coverage.BOUNDED
        assert source.calls == [(None, 30)]
        assert store.get_state("cooldown_until") is None
    finally:
        store.close()


def test_max_posts_stops_collection_at_candidate_limit() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource(
            [Page([make_post(110), make_post(109), make_post(108), make_post(107)])]
        )
        summary = run(
            store,
            source,
            clock,
            max_pages=0,
            max_posts=3,
            max_requests=5,
            request_spacing_seconds=0,
        )
        assert summary.coverage == Coverage.INCOMPLETE
        assert summary.pages == 1
        assert summary.new_count == 3
        assert source.calls == [(None, 30)]
    finally:
        store.close()


def test_partial_source_failure_retries_once_then_preserves_watermark() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource(
            [
                Page([make_post(110)], next_page="2"),
                SourceError(ErrorKind.TEMPORARY, "fixture temporary"),
                SourceError(ErrorKind.TEMPORARY, "fixture temporary"),
            ]
        )
        summary = run(store, source, clock, notifier=None)
        assert summary.error_kind == ErrorKind.TEMPORARY
        assert summary.request_count == 3
        assert summary.coverage == Coverage.INCOMPLETE
        assert clock.sleeps == [2, 5]
        assert store.baseline() == (True, "100")
        assert store.known_post_ids(["110"]) == {"110"}
        assert store.outbox_counts() == {"pending": 1}
    finally:
        store.close()


def test_request_budget_and_rate_limit_cooldown_prevent_immediate_request() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource(
            [
                Page([make_post(110)], next_page="2"),
                Page([make_post(109)], next_page="3"),
                Page([make_post(108)], next_page="4"),
            ]
        )
        limited = run(store, source, clock, max_requests=2, request_spacing_seconds=0)
        assert limited.request_count == 2
        assert limited.coverage == Coverage.INCOMPLETE
        assert store.baseline() == (True, "100")

        clock.advance(1800)
        rate_source = SequenceSource(
            [SourceError(ErrorKind.RATE_LIMIT, "fixture 429", retry_after_seconds=10)]
        )
        rate = run(store, rate_source, clock, request_spacing_seconds=0)
        assert rate.error_kind == ErrorKind.RATE_LIMIT
        assert len(rate_source.calls) == 1
        cooldown = store.get_state("cooldown_until")
        assert cooldown is not None

        clock.advance(1)
        blocked_source = SequenceSource([Page([make_post(111)], exhausted=True)])
        blocked = run(store, blocked_source, clock, request_spacing_seconds=0)
        assert blocked.skipped is True
        assert len(blocked_source.calls) == 0
    finally:
        store.close()


def test_notification_rate_limit_is_channel_wide_across_restart_and_test_attempt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
    notifier = RecordingNotifier(
        SendResult(
            SendState.PENDING,
            error_kind=ErrorKind.RATE_LIMIT,
            error_message="fixture 429",
            retry_at=clock.now() + timedelta(hours=1),
        )
    )
    with Store(database) as store:
        seed_watermark(store)
        seed_pending_batch(store, 101, "batch-101")
        seed_pending_batch(store, 102, "batch-102")
        store.set_state("next_poll_at", (clock.now() + timedelta(minutes=30)).isoformat())

        first = run(
            store,
            SequenceSource([]),
            clock,
            notifier=notifier,
            request_spacing_seconds=0,
        )
        assert first.send_state == SendState.PENDING
        assert store.notification_cooldown_until() == clock.now() + timedelta(hours=1)

    # 渠道冷却是持久化状态；重启后第二个批次仍不能触发通知请求。
    with Store(database) as restarted:
        clock.advance(1)
        second = run(
            restarted,
            SequenceSource([]),
            clock,
            notifier=notifier,
            request_spacing_seconds=0,
        )
        assert second.send_state is None
        assert second.error_kind == ErrorKind.RATE_LIMIT
        assert len(notifier.digests) == 1
        assert restarted.outbox_counts() == {"pending": 2}
        day_start, day_end = _day_bounds_for_test(clock.now())
        assert (
            restarted.start_test_attempt(
                now=clock.now(),
                daily_limit=60,
                day_start=day_start,
                day_end=day_end,
            )
            is None
        )


def test_zero_interval_allows_immediate_repeat_and_clears_next_poll(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
    with Store(database) as store:
        seed_watermark(store)
        first_source = SequenceSource([Page([make_post(101), make_post(100)], exhausted=True)])
        first = run(
            store,
            first_source,
            clock,
            interval_seconds=0,
            request_spacing_seconds=0,
        )
        assert first.skipped is False
        assert store.get_state("next_poll_at") is None

        second_source = SequenceSource([Page([make_post(102), make_post(101)], exhausted=True)])
        second = run(
            store,
            second_source,
            clock,
            interval_seconds=0,
            request_spacing_seconds=0,
        )
        assert second.skipped is False
        assert len(second_source.calls) == 1


def _day_bounds_for_test(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def test_live_preview_never_writes_production_posts_or_outbox() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource([Page([make_post(101, "preview")], exhausted=True)])
        runner = Runner(
            store,
            source,
            notifier=None,
            settings=make_settings(request_spacing_seconds=0),
            clock=clock,
            commit=False,
            send=False,
        )
        summary = runner.run_once()
        assert summary.batch_id is not None
        assert runner.last_digest is not None
        assert store.baseline() == (True, "100")
        assert store.known_post_ids(["101"]) == set()
        assert store.outbox_counts() == {}
        assert store.get_state("next_poll_at") is not None
    finally:
        store.close()


def test_contract_or_business_failure_pauses_source_until_resume() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = SequenceSource([SourceError(ErrorKind.BUSINESS, "fixture rejected")])
        summary = run(store, source, clock, request_spacing_seconds=0)
        assert summary.error_kind == ErrorKind.BUSINESS
        assert store.get_state("source_paused") == "1"
        clock.advance(1800)
        blocked_source = SequenceSource([Page([make_post(111)], exhausted=True)])
        blocked = run(store, blocked_source, clock, request_spacing_seconds=0)
        assert blocked.skipped is True
        assert len(blocked_source.calls) == 0
    finally:
        store.close()


def test_transaction_rolls_back_posts_outbox_and_watermark_together() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        post = make_post(110)
        bad_digest = Digest(
            batch_id="bad-batch",
            title="bad",
            content="bad",
            post_ids=("999",),
            created_at=datetime(2026, 9, 5, tzinfo=UTC),
            coverage=Coverage.BOUNDED,
            post_count=1,
            shown_count=1,
        )
        with pytest.raises(StoreError):
            store.commit_collection(
                now=datetime(2026, 9, 5, tzinfo=UTC),
                posts=[post],
                matched_ids={"110"},
                digest=bad_digest,
                coverage=Coverage.BOUNDED,
                proposed_watermark="110",
            )
        assert store.known_post_ids(["110"]) == set()
        assert store.outbox_counts() == {}
        assert store.baseline() == (True, "100")
    finally:
        store.close()


def test_lock_is_kernel_lock_and_releases_on_context_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    first = ProcessLock(lock_path)
    second = ProcessLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(LockBusy):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_cleanup_retains_unbounded_above_watermark_ids() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store, "100")
        now = datetime(2026, 9, 20, tzinfo=UTC)
        store.commit_collection(
            now=now - timedelta(days=10),
            posts=[make_post(90), make_post(110)],
            matched_ids=set(),
            digest=None,
            coverage=Coverage.INCOMPLETE,
            proposed_watermark=None,
        )
        store.cleanup(now=now, post_retention=timedelta(days=7), audit_retention=timedelta(days=30))
        assert store.known_post_ids(["90"]) == set()
        assert store.known_post_ids(["110"]) == {"110"}
    finally:
        store.close()


@pytest.mark.parametrize("terminal_state", [SendState.ACCEPTED, SendState.EXPIRED])
def test_cleanup_retains_above_watermark_ids_for_terminal_batches(
    terminal_state: SendState,
) -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store, "100")
        created = datetime(2026, 9, 5, tzinfo=UTC)
        post = make_post(110)
        store.commit_collection(
            now=created,
            posts=[post],
            matched_ids={post.id},
            digest=Digest(
                batch_id=f"batch-{terminal_state.value}",
                title="终态批次",
                content="正文片段",
                post_ids=(post.id,),
                created_at=created,
                coverage=Coverage.INCOMPLETE,
                post_count=1,
                shown_count=1,
            ),
            coverage=Coverage.INCOMPLETE,
            proposed_watermark=None,
        )
        batch_id = f"batch-{terminal_state.value}"
        day_start, day_end = _day_bounds_for_test(created)
        if terminal_state == SendState.ACCEPTED:
            claim = store.claim_outbox(
                now=created,
                daily_limit=60,
                day_start=day_start,
                day_end=day_end,
                pending_ttl=timedelta(days=1),
            ).claim
            assert claim is not None
            store.finish_send(
                claim.attempt_id,
                created,
                SendResult(SendState.ACCEPTED, provider_receipt="fixture"),
            )
        else:
            assert (
                store.claim_outbox(
                    now=created + timedelta(days=10),
                    daily_limit=60,
                    day_start=day_start,
                    day_end=day_end,
                    pending_ttl=timedelta(days=7),
                ).claim
                is None
            )
        store.cleanup(
            now=created + timedelta(days=10),
            post_retention=timedelta(days=7),
            audit_retention=timedelta(days=30),
        )
        assert store.known_post_ids(["110"]) == {"110"}
        assert store.posts_for_batch(batch_id)[0]["snippet"] == ""
    finally:
        store.close()


def test_normal_run_once_executes_periodic_cleanup_and_keeps_unresolved_batch() -> None:
    clock = FakeClock(datetime(2026, 9, 15, tzinfo=UTC))
    store = Store(":memory:")
    try:
        seed_watermark(store, "100")
        old = clock.now() - timedelta(days=10)
        accepted = make_post(90, "old accepted")
        unresolved = make_post(91, "old pending")
        for post, batch_id in ((accepted, "accepted-old"), (unresolved, "pending-old")):
            store.commit_collection(
                now=old,
                posts=[post],
                matched_ids={post.id},
                digest=Digest(
                    batch_id=batch_id,
                    title=batch_id,
                    content="old content",
                    post_ids=(post.id,),
                    created_at=old,
                    coverage=Coverage.INCOMPLETE,
                    post_count=1,
                    shown_count=1,
                ),
                coverage=Coverage.INCOMPLETE,
                proposed_watermark=None,
            )
        day_start, day_end = _day_bounds_for_test(old)
        accepted_claim = store.claim_outbox(
            now=old,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(days=1),
        ).claim
        assert accepted_claim is not None
        store.finish_send(
            accepted_claim.attempt_id,
            old,
            SendResult(SendState.ACCEPTED, provider_receipt="fixture"),
        )

        summary = run(
            store,
            SequenceSource([Page([make_post(100)], exhausted=True)]),
            clock,
            notifier=None,
            request_spacing_seconds=0,
        )
        assert summary.coverage == Coverage.BOUNDED
        assert store.get_state("last_cleanup_at") == clock.now().isoformat()
        assert store.known_post_ids(["90"]) == set()
        assert store.outbox("accepted-old")["content"] is None
        assert store.known_post_ids(["91"]) == {"91"}
        assert store.outbox("pending-old")["status"] == SendState.PENDING.value
        assert store.outbox("pending-old")["content"] == "old content"
    finally:
        store.close()


def test_cleanup_removes_old_terminal_audit_but_preserves_above_watermark_id() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store, "100")
        created = datetime(2026, 8, 1, tzinfo=UTC)
        post = make_post(110, "old terminal")
        batch_id = "terminal-over-thirty-days"
        store.commit_collection(
            now=created,
            posts=[post],
            matched_ids={post.id},
            digest=Digest(
                batch_id=batch_id,
                title="旧终态批次",
                content="旧消息正文",
                post_ids=(post.id,),
                created_at=created,
                coverage=Coverage.INCOMPLETE,
                post_count=1,
                shown_count=1,
            ),
            coverage=Coverage.INCOMPLETE,
            proposed_watermark=None,
        )
        day_start, day_end = _day_bounds_for_test(created)
        claim = store.claim_outbox(
            now=created,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(days=1),
        ).claim
        assert claim is not None
        store.finish_send(
            claim.attempt_id,
            created,
            SendResult(SendState.ACCEPTED, provider_receipt="fixture"),
        )

        cleanup = store.cleanup(
            now=datetime(2026, 9, 5, tzinfo=UTC),
            post_retention=timedelta(days=7),
            audit_retention=timedelta(days=30),
        )
        assert cleanup.outbox_deleted == 1
        assert store.outbox(batch_id) is None
        assert store.known_post_ids(["110"]) == {"110"}
    finally:
        store.close()


def test_source_response_after_run_budget_is_incomplete_and_not_committed() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        source = AdvancingSource(clock, 200, Page([make_post(101)], exhausted=True))
        notifier = RecordingNotifier()
        summary = run(store, source, clock, notifier=notifier, request_spacing_seconds=0)
        assert summary.coverage == Coverage.INCOMPLETE
        assert summary.error_kind == ErrorKind.TEMPORARY
        assert summary.elapsed_seconds == 200
        assert notifier.digests == []
        assert store.known_post_ids(["101"]) == set()
        assert store.outbox_counts() == {}
    finally:
        store.close()


def test_expired_budget_after_commit_does_not_start_notification() -> None:
    clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
    store = AdvancingCommitStore(clock, 180)
    try:
        seed_watermark(store)
        notifier = RecordingNotifier()
        summary = run(
            store,
            SequenceSource([Page([make_post(101, "new")], exhausted=True)]),
            clock,
            notifier=notifier,
            request_spacing_seconds=0,
        )
        assert summary.coverage == Coverage.BOUNDED
        assert summary.error_kind == ErrorKind.TEMPORARY
        assert summary.send_state is None
        assert summary.elapsed_seconds == 180
        assert notifier.digests == []
        assert store.outbox_counts() == {"pending": 1}
    finally:
        store.close()


def test_elapsed_includes_send_and_late_acceptance_becomes_unknown() -> None:
    store = Store(":memory:")
    try:
        seed_watermark(store)
        clock = FakeClock(datetime(2026, 9, 5, tzinfo=UTC))
        notifier = AdvancingNotifier(
            clock,
            200,
            SendResult(SendState.ACCEPTED, provider_receipt="late-receipt"),
        )
        summary = run(
            store,
            SequenceSource([Page([make_post(101, "new")], exhausted=True)]),
            clock,
            notifier=notifier,
            request_spacing_seconds=0,
        )
        assert summary.elapsed_seconds == 200
        assert summary.send_state == SendState.UNKNOWN
        assert store.outbox_counts() == {"unknown": 1}
    finally:
        store.close()
