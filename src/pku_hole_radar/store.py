from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Coverage, Digest, ErrorKind, Post, SendClaim, SendResult, SendState

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """本地数据库状态错误。"""


@dataclass(frozen=True, slots=True)
class CollectionCommit:
    inserted_ids: tuple[str, ...]
    batch_id: str | None
    watermark: str | None


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    claim: SendClaim | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    content_cleared: int
    snippets_cleared: int
    attempts_deleted: int
    posts_deleted: int
    outbox_deleted: int
    has_more: bool


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        database_path: Path | None = None
        database_preexisting = True
        if self.path != ":memory:":
            database_path = Path(self.path).expanduser().resolve()
            database_preexisting = database_path.exists()
            parent = database_path.parent
            parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
            if not database_preexisting and database_path is not None:
                database_path.chmod(0o600)
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def all_state(self) -> dict[str, str]:
        rows = self.connection.execute("SELECT key, value FROM state ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_state(self, key: str, value: str | None) -> None:
        with self.transaction() as connection:
            _set_state(connection, key, value)

    def set_states(self, values: dict[str, str | None]) -> None:
        with self.transaction() as connection:
            for key, value in values.items():
                _set_state(connection, key, value)

    def baseline(self) -> tuple[bool, str | None]:
        initialized = self.get_state("baseline_initialized", "0") == "1"
        watermark = self.get_state("watermark_id")
        return initialized, watermark or None

    def recover_sending(self, now: datetime) -> int:
        now_text = _iso(now)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT attempt_id, batch_id FROM send_attempts WHERE result = 'started'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE send_attempts
                    SET result = 'unknown', finished_at = ?, error_kind = ?, error_message = ?
                    WHERE attempt_id = ? AND result = 'started'
                    """,
                    (
                        now_text,
                        ErrorKind.PROCESS_CRASH.value,
                        "进程在发送期间退出",
                        row["attempt_id"],
                    ),
                )
                if row["batch_id"] is not None:
                    connection.execute(
                        """
                        UPDATE outbox
                        SET status = 'unknown', last_error_kind = ?, updated_at = ?
                        WHERE batch_id = ? AND status = 'sending'
                        """,
                        (ErrorKind.PROCESS_CRASH.value, now_text, row["batch_id"]),
                    )
            return len(rows)

    def commit_collection(
        self,
        *,
        now: datetime,
        posts: Sequence[Post],
        matched_ids: set[str],
        digest: Digest | None,
        coverage: Coverage,
        proposed_watermark: str | None,
        baseline: bool = False,
    ) -> CollectionCommit:
        """原子提交帖子、批次关联和水位；调用方不得在事务外推进水位。"""

        now_text = _iso(now)
        post_ids = tuple(dict.fromkeys(post.id for post in posts))
        if len(post_ids) != len(posts):
            posts = _dedupe_posts(posts)
            post_ids = tuple(post.id for post in posts)
        with self.transaction() as connection:
            existing = set()
            if post_ids:
                placeholders = ",".join("?" for _ in post_ids)
                rows = connection.execute(
                    f"SELECT id FROM posts WHERE id IN ({placeholders})", post_ids
                ).fetchall()
                existing = {str(row["id"]) for row in rows}

            inserted_ids: list[str] = []
            for post in posts:
                if post.id in existing:
                    continue
                connection.execute(
                    """
                    INSERT INTO posts
                    (id, created_at, first_seen_at, snippet, url, matched, batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        post.id,
                        _iso(post.created_at),
                        now_text,
                        _stored_snippet(post.text, post.has_media),
                        post.url,
                        1 if post.id in matched_ids else 0,
                    ),
                )
                inserted_ids.append(post.id)

            batch_id = None
            if digest is not None:
                new_matched = [post_id for post_id in digest.post_ids if post_id in inserted_ids]
                if set(new_matched) != set(digest.post_ids):
                    raise StoreError("digest 包含已入库帖子，拒绝重新分配通知批次")
                batch_id = digest.batch_id
                connection.execute(
                    """
                    INSERT INTO outbox
                    (batch_id, title, content, post_count, shown_count, status, attempts,
                     next_attempt_at, provider_receipt, created_at, updated_at, last_error_kind)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        digest.batch_id,
                        digest.title,
                        digest.content,
                        digest.post_count,
                        digest.shown_count,
                        SendState.PENDING.value,
                        now_text,
                        now_text,
                        now_text,
                    ),
                )
                for post_id in digest.post_ids:
                    connection.execute(
                        "UPDATE posts SET batch_id = ? WHERE id = ? AND batch_id IS NULL",
                        (digest.batch_id, post_id),
                    )

            if baseline:
                _set_state(connection, "baseline_initialized", "1")
                _set_state(connection, "baseline_id", proposed_watermark or "")
                _set_state(connection, "watermark_id", proposed_watermark or "")
                _set_state(connection, "coverage", Coverage.BASELINE.value)
            elif coverage == Coverage.BOUNDED and proposed_watermark is not None:
                _set_state(connection, "watermark_id", proposed_watermark)
                _set_state(connection, "coverage", coverage.value)
            else:
                _set_state(connection, "coverage", coverage.value)

            return CollectionCommit(tuple(inserted_ids), batch_id, proposed_watermark)

    def claim_outbox(
        self,
        *,
        now: datetime,
        daily_limit: int,
        day_start: datetime,
        day_end: datetime,
        pending_ttl: timedelta,
        max_attempts: int = 3,
    ) -> ClaimDecision:
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正数")
        now_text = _iso(now)
        day_start_text = _iso(day_start)
        day_end_text = _iso(day_end)
        with self.transaction() as connection:
            cutoff = _iso(now - pending_ttl)
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, last_error_kind = ?, updated_at = ?
                WHERE status = ? AND created_at < ?
                """,
                (
                    SendState.EXPIRED.value,
                    "pending_ttl",
                    now_text,
                    SendState.PENDING.value,
                    cutoff,
                ),
            )
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, last_error_kind = ?, next_attempt_at = NULL, updated_at = ?
                WHERE status = ? AND attempts >= ?
                  AND COALESCE(last_error_kind, '') != 'manual_retry_acknowledged'
                """,
                (
                    SendState.FAILED.value,
                    "max_attempts",
                    now_text,
                    SendState.PENDING.value,
                    max_attempts,
                ),
            )
            if _notification_cooldown_active(connection, now):
                return ClaimDecision(None, "notification_channel_cooldown")
            attempts_today = connection.execute(
                """
                SELECT COUNT(*) AS count FROM send_attempts
                WHERE started_at >= ? AND started_at < ?
                """,
                (day_start_text, day_end_text),
            ).fetchone()["count"]
            if attempts_today >= daily_limit:
                return ClaimDecision(None, "daily_send_budget_exhausted")

            row = connection.execute(
                """
                SELECT batch_id, title, content, attempts, created_at
                FROM outbox
                WHERE status = ? AND (attempts < ? OR last_error_kind = 'manual_retry_acknowledged')
                  AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, created_at ASC
                LIMIT 1
                """,
                (SendState.PENDING.value, max_attempts, now_text),
            ).fetchone()
            if row is None:
                return ClaimDecision(None, "no_due_outbox")
            attempt_id = uuid.uuid4().hex
            new_attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, attempts = ?, next_attempt_at = NULL, updated_at = ?
                WHERE batch_id = ? AND status = ?
                """,
                (
                    SendState.SENDING.value,
                    new_attempts,
                    now_text,
                    row["batch_id"],
                    SendState.PENDING.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO send_attempts
                (attempt_id, batch_id, started_at, finished_at, result, provider_receipt,
                 error_kind, error_message)
                VALUES (?, ?, ?, NULL, 'started', NULL, NULL, NULL)
                """,
                (attempt_id, row["batch_id"], now_text),
            )
            return ClaimDecision(
                SendClaim(
                    batch_id=str(row["batch_id"]),
                    attempt_id=attempt_id,
                    title=str(row["title"]),
                    content=str(row["content"] or ""),
                    attempts=new_attempts,
                    created_at=_parse_iso(str(row["created_at"])),
                )
            )

    def start_test_attempt(
        self,
        *,
        now: datetime,
        daily_limit: int,
        day_start: datetime,
        day_end: datetime,
    ) -> str | None:
        with self.transaction() as connection:
            if _notification_cooldown_active(connection, now):
                return None
            count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM send_attempts
                WHERE started_at >= ? AND started_at < ?
                """,
                (_iso(day_start), _iso(day_end)),
            ).fetchone()["count"]
            if count >= daily_limit:
                return None
            attempt_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO send_attempts
                (attempt_id, batch_id, started_at, finished_at, result, provider_receipt,
                 error_kind, error_message)
                VALUES (?, NULL, ?, NULL, 'started', NULL, NULL, NULL)
                """,
                (attempt_id, _iso(now)),
            )
            return attempt_id

    def finish_send(self, attempt_id: str, now: datetime, result: SendResult) -> None:
        if result.state in {SendState.SENDING, SendState.EXPIRED, SendState.DISCARDED}:
            raise StoreError(f"不能将发送尝试写成状态 {result.state.value}")
        now_text = _iso(now)
        with self.transaction() as connection:
            attempt = connection.execute(
                "SELECT batch_id, result FROM send_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise StoreError("发送尝试不存在")
            if attempt["result"] != "started":
                raise StoreError("发送尝试已经结束，拒绝重复写入结果")
            connection.execute(
                """
                UPDATE send_attempts
                SET finished_at = ?, result = ?, provider_receipt = ?,
                    error_kind = ?, error_message = ?
                WHERE attempt_id = ?
                """,
                (
                    now_text,
                    result.state.value,
                    result.provider_receipt,
                    result.error_kind.value if result.error_kind else None,
                    result.error_message,
                    attempt_id,
                ),
            )
            batch_id = attempt["batch_id"]
            current_now = _parse_iso(now_text)
            if result.error_kind == ErrorKind.RATE_LIMIT:
                fallback_until = current_now + timedelta(hours=1)
                requested_until = (
                    _parse_iso(_iso(result.retry_at)) if result.retry_at else fallback_until
                )
                minimum_until = current_now + timedelta(minutes=30)
                current_until = _notification_cooldown_from_connection(connection)
                cooldown_until = max(minimum_until, requested_until, current_until or current_now)
                _set_state(connection, "notify_cooldown_until", _iso(cooldown_until))
                _set_state(connection, "notify_cooldown_reason", "通知渠道返回频率限制")
            elif result.state in {SendState.ACCEPTED, SendState.DELIVERED}:
                current_until = _notification_cooldown_from_connection(connection)
                if current_until is not None and current_until <= current_now:
                    _set_state(connection, "notify_cooldown_until", None)
                    _set_state(connection, "notify_cooldown_reason", None)
            if batch_id is None:
                return
            next_attempt = _iso(result.retry_at) if result.retry_at else None
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, provider_receipt = COALESCE(?, provider_receipt),
                    next_attempt_at = ?, last_error_kind = ?, updated_at = ?
                WHERE batch_id = ? AND status = ?
                """,
                (
                    result.state.value,
                    result.provider_receipt,
                    next_attempt,
                    result.error_kind.value if result.error_kind else None,
                    now_text,
                    batch_id,
                    SendState.SENDING.value,
                ),
            )

    def retry_outbox(self, batch_id: str, now: datetime, *, acknowledge_duplicate: bool) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise StoreError("批次不存在")
            status = SendState(str(row["status"]))
            if status == SendState.UNKNOWN and not acknowledge_duplicate:
                raise StoreError("unknown 批次重试必须显式提供 --ack-possible-duplicate")
            if status not in {SendState.UNKNOWN, SendState.FAILED}:
                raise StoreError(f"当前状态 {status.value} 不允许人工重试")
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, next_attempt_at = ?, last_error_kind = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    SendState.PENDING.value,
                    _iso(now),
                    "manual_retry_acknowledged",
                    _iso(now),
                    batch_id,
                ),
            )

    def discard_outbox(self, batch_id: str, now: datetime) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise StoreError("批次不存在")
            status = SendState(str(row["status"]))
            if status not in {SendState.UNKNOWN, SendState.FAILED}:
                raise StoreError(f"当前状态 {status.value} 不允许丢弃")
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, next_attempt_at = NULL, last_error_kind = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (SendState.DISCARDED.value, "manual_discard", _iso(now), batch_id),
            )

    def reset_baseline(self, *, now: datetime, posts: Sequence[Post], watermark: str | None) -> int:
        now_text = _iso(now)
        with self.transaction() as connection:
            inserted = 0
            for post in _dedupe_posts(posts):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO posts
                    (id, created_at, first_seen_at, snippet, url, matched, batch_id)
                    VALUES (?, ?, ?, ?, ?, 0, NULL)
                    """,
                    (
                        post.id,
                        _iso(post.created_at),
                        now_text,
                        _stored_snippet(post.text, post.has_media),
                        post.url,
                    ),
                )
                inserted += cursor.rowcount
            _set_state(connection, "baseline_initialized", "1")
            _set_state(connection, "baseline_id", watermark or "")
            _set_state(connection, "watermark_id", watermark or "")
            _set_state(connection, "coverage", Coverage.BASELINE.value)
            _set_state(connection, "baseline_reset_at", now_text)
            _set_state(connection, "baseline_reset_known_max_id", watermark or "")
            return inserted

    def posts_for_batch(self, batch_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT id, created_at, first_seen_at, snippet, url, matched, batch_id
            FROM posts WHERE batch_id = ? ORDER BY CAST(id AS INTEGER) DESC
            """,
            (batch_id,),
        ).fetchall()

    def outbox(self, batch_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM outbox WHERE batch_id = ?", (batch_id,)
        ).fetchone()

    def outbox_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def send_attempt_count(self, day_start: datetime, day_end: datetime) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*) AS count FROM send_attempts
                WHERE started_at >= ? AND started_at < ?
                """,
                (_iso(day_start), _iso(day_end)),
            ).fetchone()["count"]
        )

    def notification_cooldown_until(self) -> datetime | None:
        return _notification_cooldown_from_value(self.get_state("notify_cooldown_until"))

    def cleanup(
        self,
        *,
        now: datetime,
        post_retention: timedelta,
        audit_retention: timedelta,
        limit: int = 500,
    ) -> CleanupResult:
        """有界清理终态内容和审计；未决批次及水位以上 ID 永不删除。"""

        if limit <= 0:
            raise ValueError("cleanup limit 必须为正数")

        post_cutoff = _iso(now - post_retention)
        audit_cutoff = _iso(now - audit_retention)
        with self.transaction() as connection:
            content_cursor = connection.execute(
                """
                UPDATE outbox SET content = NULL
                WHERE rowid IN (
                    SELECT rowid FROM outbox
                    WHERE status IN ('accepted', 'delivered', 'expired', 'discarded')
                      AND created_at < ? AND content IS NOT NULL
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (post_cutoff, limit),
            )
            content_cleared = content_cursor.rowcount

            # 终态帖子 7 天后可以清空片段，但不能把未决批次的正文清掉。
            snippet_cursor = connection.execute(
                """
                UPDATE posts SET snippet = ''
                WHERE rowid IN (
                    SELECT p.rowid
                    FROM posts AS p
                    LEFT JOIN outbox AS o ON o.batch_id = p.batch_id
                    WHERE p.first_seen_at < ?
                      AND (p.batch_id IS NULL
                           OR o.status IN ('accepted', 'delivered', 'expired', 'discarded'))
                      AND p.snippet != ''
                    ORDER BY p.first_seen_at ASC
                    LIMIT ?
                )
                """,
                (post_cutoff, limit),
            )
            snippets_cleared = snippet_cursor.rowcount

            # 未决批次的尝试是恢复和人工判断的证据，不能因审计窗口直接删除。
            attempts_cursor = connection.execute(
                """
                DELETE FROM send_attempts
                WHERE rowid IN (
                    SELECT a.rowid
                    FROM send_attempts AS a
                    LEFT JOIN outbox AS o ON o.batch_id = a.batch_id
                    WHERE a.started_at < ?
                      AND (a.batch_id IS NULL OR o.batch_id IS NULL
                           OR o.status IN ('accepted', 'delivered', 'expired', 'discarded'))
                    ORDER BY a.started_at ASC
                    LIMIT ?
                )
                """,
                (audit_cutoff, limit),
            )
            attempts_deleted = attempts_cursor.rowcount

            watermark = self.get_state("watermark_id")
            posts_deleted = 0
            if watermark:
                # 无论是否已经关联终态批次，水位以上 ID 都是覆盖不完整时的去重证据。
                posts_cursor = connection.execute(
                    """
                    DELETE FROM posts
                    WHERE rowid IN (
                        SELECT p.rowid
                        FROM posts AS p
                        LEFT JOIN outbox AS o ON o.batch_id = p.batch_id
                        WHERE p.first_seen_at < ?
                          AND (p.batch_id IS NULL
                               OR o.status IN ('accepted', 'delivered', 'expired', 'discarded'))
                          AND CAST(p.id AS INTEGER) <= CAST(? AS INTEGER)
                        ORDER BY p.first_seen_at ASC
                        LIMIT ?
                    )
                    """,
                    (post_cutoff, watermark, limit),
                )
                posts_deleted = posts_cursor.rowcount

            # 30 天后删除终态 outbox；先解除帖子外键，未决批次不会进入这个集合。
            connection.execute(
                """
                UPDATE posts SET batch_id = NULL
                WHERE batch_id IN (
                    SELECT batch_id FROM outbox
                    WHERE status IN ('accepted', 'delivered', 'expired', 'discarded')
                      AND created_at < ?
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (audit_cutoff, limit),
            )
            outbox_cursor = connection.execute(
                """
                DELETE FROM outbox
                WHERE rowid IN (
                    SELECT rowid FROM outbox
                    WHERE status IN ('accepted', 'delivered', 'expired', 'discarded')
                      AND created_at < ?
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (audit_cutoff, limit),
            )
            outbox_deleted = outbox_cursor.rowcount

            has_more = any(
                count >= limit
                for count in (
                    content_cleared,
                    snippets_cleared,
                    attempts_deleted,
                    posts_deleted,
                    outbox_deleted,
                )
            )
            return CleanupResult(
                content_cleared=content_cleared,
                snippets_cleared=snippets_cleared,
                attempts_deleted=attempts_deleted,
                posts_deleted=posts_deleted,
                outbox_deleted=outbox_deleted,
                has_more=has_more,
            )

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def known_post_ids(self, post_ids: Sequence[str]) -> set[str]:
        values = tuple(dict.fromkeys(post_ids))
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        rows = self.connection.execute(
            f"SELECT id FROM posts WHERE id IN ({placeholders})", values
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def latest_outbox_status(self, batch_id: str) -> SendState | None:
        row = self.connection.execute(
            "SELECT status FROM outbox WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return None if row is None else SendState(str(row["status"]))

    def list_outbox(
        self, *, status: SendState | str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        if limit <= 0:
            raise ValueError("limit 必须为正数")
        conditions = []
        parameters: list[object] = []
        if status is not None:
            status_value = status.value if isinstance(status, SendState) else str(status)
            try:
                SendState(status_value)
            except ValueError as exc:
                raise ValueError(f"未知 outbox 状态：{status_value}") from exc
            conditions.append("o.status = ?")
            parameters.append(status_value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(limit)
        return self.connection.execute(
            f"""
            SELECT o.*,
                   (
                       SELECT a.error_message
                       FROM send_attempts AS a
                       WHERE a.batch_id = o.batch_id
                       ORDER BY a.started_at DESC, a.attempt_id DESC
                       LIMIT 1
                   ) AS last_error_message,
                   (
                       SELECT a.result
                       FROM send_attempts AS a
                       WHERE a.batch_id = o.batch_id
                       ORDER BY a.started_at DESC, a.attempt_id DESC
                       LIMIT 1
                   ) AS last_attempt_result
            FROM outbox AS o
            {where}
            ORDER BY o.created_at DESC, o.batch_id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError(f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}")
        if version == 0:
            self._create_schema()

    def _create_schema(self) -> None:
        """使用显式短事务创建初始 schema，避免 executescript 隐式提交外层事务。"""

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    batch_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT,
                    post_count INTEGER NOT NULL CHECK (post_count >= 0),
                    shown_count INTEGER NOT NULL CHECK (shown_count >= 0),
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_at TEXT,
                    provider_receipt TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error_kind TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    url TEXT NOT NULL,
                    matched INTEGER NOT NULL CHECK (matched IN (0, 1)),
                    batch_id TEXT REFERENCES outbox(batch_id)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS send_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    batch_id TEXT REFERENCES outbox(batch_id) ON DELETE SET NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT NOT NULL,
                    provider_receipt TEXT,
                    error_kind TEXT,
                    error_message TEXT
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_batch_id ON posts(batch_id)"
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON outbox(status, next_attempt_at, created_at)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_send_attempts_started_at
                ON send_attempts(started_at)
                """
            )
            self.connection.execute("PRAGMA user_version = 1")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


def _set_state(connection: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        connection.execute("DELETE FROM state WHERE key = ?", (key,))
    else:
        connection.execute(
            """
            INSERT INTO state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _notification_cooldown_from_value(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_iso(value)
    except ValueError:
        return None


def _notification_cooldown_from_connection(
    connection: sqlite3.Connection,
) -> datetime | None:
    row = connection.execute(
        "SELECT value FROM state WHERE key = 'notify_cooldown_until'"
    ).fetchone()
    return _notification_cooldown_from_value(None if row is None else str(row["value"]))


def _notification_cooldown_active(connection: sqlite3.Connection, now: datetime) -> bool:
    until = _notification_cooldown_from_connection(connection)
    current = _parse_iso(_iso(now))
    return until is not None and until > current


def _stored_snippet(text: str, has_media: bool) -> str:
    if not text.strip() and has_media:
        return "图片帖"
    clean = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")
    clean = " ".join(clean.split())
    return clean[:120]


def _dedupe_posts(posts: Sequence[Post]) -> list[Post]:
    result: list[Post] = []
    seen: set[str] = set()
    for post in posts:
        if post.id not in seen:
            result.append(post)
            seen.add(post.id)
    return result
