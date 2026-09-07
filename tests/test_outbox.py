from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pku_hole_radar.models import Coverage, Digest, ErrorKind, Post, SendResult, SendState
from pku_hole_radar.store import Store, StoreError

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def post(post_id: str = "101") -> Post:
    return Post(
        id=post_id,
        created_at=NOW,
        text="fixture",
        url=f"https://fixture.test/post/{post_id}",
    )


def digest(post_id: str = "101", batch_id: str = "batch-101") -> Digest:
    return Digest(
        batch_id=batch_id,
        title="树洞雷达｜1 条新帖",
        content="正文",
        post_ids=(post_id,),
        created_at=NOW,
        coverage=Coverage.BOUNDED,
        post_count=1,
        shown_count=1,
    )


def day_bounds() -> tuple[datetime, datetime]:
    return datetime(2026, 9, 5, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC)


def make_batch(
    store: Store,
    *,
    created_at: datetime = NOW,
    post_id: str = "101",
    batch_id: str = "batch-101",
) -> None:
    store.commit_collection(
        now=created_at,
        posts=[post(post_id)],
        matched_ids={post_id},
        digest=digest(post_id, batch_id),
        coverage=Coverage.BOUNDED,
        proposed_watermark="101",
    )


def test_unknown_after_restart_is_not_auto_retried_and_requires_ack() -> None:
    store = Store(":memory:")
    try:
        make_batch(store)
        day_start, day_end = day_bounds()
        claim = store.claim_outbox(
            now=NOW,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        ).claim
        assert claim is not None
        assert store.outbox("batch-101")["status"] == SendState.SENDING.value

        assert store.recover_sending(NOW + timedelta(minutes=1)) == 1
        assert store.outbox("batch-101")["status"] == SendState.UNKNOWN.value
        assert (
            store.claim_outbox(
                now=NOW + timedelta(minutes=1),
                daily_limit=60,
                day_start=day_start,
                day_end=day_end,
                pending_ttl=timedelta(hours=24),
            ).claim
            is None
        )
        with pytest.raises(StoreError, match="ack-possible-duplicate"):
            store.retry_outbox("batch-101", NOW, acknowledge_duplicate=False)
        store.retry_outbox("batch-101", NOW, acknowledge_duplicate=True)
        retried = store.claim_outbox(
            now=NOW,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        ).claim
        assert retried is not None
        store.finish_send(
            retried.attempt_id,
            NOW,
            SendResult(SendState.ACCEPTED, provider_receipt="receipt-2"),
        )
        assert store.outbox("batch-101")["status"] == SendState.ACCEPTED.value
        assert store.send_attempt_count(day_start, day_end) == 2
    finally:
        store.close()


def test_pending_retry_waits_and_max_automatic_attempts_becomes_failed() -> None:
    store = Store(":memory:")
    try:
        make_batch(store)
        day_start, day_end = day_bounds()
        current = NOW
        for _attempt_number in range(3):
            claim = store.claim_outbox(
                now=current,
                daily_limit=60,
                day_start=day_start,
                day_end=day_end,
                pending_ttl=timedelta(hours=24),
            ).claim
            assert claim is not None
            store.finish_send(
                claim.attempt_id,
                current,
                SendResult(
                    SendState.PENDING,
                    error_kind=ErrorKind.TEMPORARY_NOT_SENT,
                    retry_at=current + timedelta(minutes=30),
                ),
            )
            assert (
                store.claim_outbox(
                    now=current + timedelta(minutes=29),
                    daily_limit=60,
                    day_start=day_start,
                    day_end=day_end,
                    pending_ttl=timedelta(hours=24),
                ).claim
                is None
            )
            current += timedelta(minutes=30)
        assert store.outbox("batch-101")["status"] == SendState.FAILED.value
    finally:
        store.close()


def test_daily_budget_counts_test_and_failed_attempts() -> None:
    store = Store(":memory:")
    try:
        day_start, day_end = day_bounds()
        attempt_id = store.start_test_attempt(
            now=NOW,
            daily_limit=1,
            day_start=day_start,
            day_end=day_end,
        )
        assert attempt_id is not None
        store.finish_send(
            attempt_id,
            NOW,
            SendResult(
                SendState.FAILED,
                error_kind=ErrorKind.BUSINESS,
                error_message="拒绝",
            ),
        )
        make_batch(store)
        decision = store.claim_outbox(
            now=NOW,
            daily_limit=1,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        )
        assert decision.claim is None
        assert decision.reason == "daily_send_budget_exhausted"
        assert store.send_attempt_count(day_start, day_end) == 1
    finally:
        store.close()


def test_pending_batch_expires_after_ttl() -> None:
    store = Store(":memory:")
    try:
        make_batch(store, created_at=NOW - timedelta(hours=25))
        day_start, day_end = day_bounds()
        decision = store.claim_outbox(
            now=NOW,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        )
        assert decision.claim is None
        assert store.outbox("batch-101")["status"] == SendState.EXPIRED.value
    finally:
        store.close()


def test_channel_cooldown_also_blocks_a_manually_requeued_unknown_batch() -> None:
    store = Store(":memory:")
    try:
        make_batch(store)
        day_start, day_end = day_bounds()
        claim = store.claim_outbox(
            now=NOW,
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        ).claim
        assert claim is not None
        assert store.recover_sending(NOW + timedelta(seconds=1)) == 1

        test_attempt = store.start_test_attempt(
            now=NOW + timedelta(seconds=1),
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
        )
        assert test_attempt is not None
        store.finish_send(
            test_attempt,
            NOW + timedelta(seconds=1),
            SendResult(
                SendState.PENDING,
                error_kind=ErrorKind.RATE_LIMIT,
                retry_at=NOW + timedelta(hours=1),
            ),
        )
        store.retry_outbox("batch-101", NOW + timedelta(seconds=2), acknowledge_duplicate=True)
        decision = store.claim_outbox(
            now=NOW + timedelta(seconds=2),
            daily_limit=60,
            day_start=day_start,
            day_end=day_end,
            pending_ttl=timedelta(hours=24),
        )
        assert decision.claim is None
        assert decision.reason == "notification_channel_cooldown"
        assert store.outbox("batch-101")["status"] == SendState.PENDING.value
    finally:
        store.close()
