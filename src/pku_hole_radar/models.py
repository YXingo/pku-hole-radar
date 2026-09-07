from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Coverage(StrEnum):
    BASELINE = "baseline"
    BOUNDED = "bounded"
    INCOMPLETE = "incomplete"


class SendState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"
    FAILED = "failed"
    EXPIRED = "expired"
    DISCARDED = "discarded"


class ErrorKind(StrEnum):
    CONFIG = "config"
    AUTH = "auth"
    ACCESS_DENIED = "access_denied"
    RATE_LIMIT = "rate_limit"
    BUSINESS = "business"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    TEMPORARY_NOT_SENT = "temporary_not_sent"
    UNKNOWN = "unknown"
    PROCESS_CRASH = "process_crash"
    MALFORMED_SUCCESS = "malformed_success"
    QUOTA = "quota"
    PERMANENT = "permanent"


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    created_at: datetime
    text: str
    url: str
    is_pinned: bool = False
    has_media: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.id.isascii() or not self.id.isdecimal():
            raise ValueError("帖子 ID 必须是非空十进制字符串")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class Page:
    posts: list[Post]
    next_page: str | None = None
    exhausted: bool = False
    total: int | None = None


@dataclass(frozen=True, slots=True)
class Digest:
    batch_id: str
    title: str
    content: str
    post_ids: tuple[str, ...]
    created_at: datetime
    coverage: Coverage
    post_count: int
    shown_count: int


@dataclass(frozen=True, slots=True)
class SendResult:
    state: SendState
    provider_receipt: str | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FetchSummary:
    posts: tuple[Post, ...]
    candidate_posts: tuple[Post, ...]
    coverage: Coverage
    request_count: int
    pages: int
    proposed_watermark: str | None
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    retry_after_seconds: int | None = None
    baseline: bool = False


@dataclass(frozen=True, slots=True)
class SendClaim:
    batch_id: str
    attempt_id: str
    title: str
    content: str
    attempts: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    pages: int = 0
    request_count: int = 0
    new_count: int = 0
    matched_count: int = 0
    coverage: Coverage | None = None
    batch_id: str | None = None
    batch_state: SendState | None = None
    send_state: SendState | None = None
    skipped: bool = False
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)
