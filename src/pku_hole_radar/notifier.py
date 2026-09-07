from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from .models import Digest, ErrorKind, SendResult, SendState


class Notifier(Protocol):
    def send(self, digest: Digest) -> SendResult:
        """发送一个不可变简报，并明确说明结果是否可确认。"""


class StdoutNotifier:
    """离线预览接收器；accepted 只代表 stdout 已接收，不代表微信送达。"""

    def send(self, digest: Digest) -> SendResult:
        print(digest.title)
        print(digest.content)
        return SendResult(state=SendState.ACCEPTED, provider_receipt="stdout")


class PushPlusNotifier:
    """PushPlus 微信渠道适配器。

    官方发送接口只返回服务端受理流水号；本适配器不会把它冒称为 delivered，
    也不在本地实现 callback Web 服务或保存 access-key。
    """

    ENDPOINT = "https://www.pushplus.plus/send"

    def __init__(
        self,
        token: str,
        *,
        channel: str = "wechat",
        client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        if not token.strip():
            raise ValueError("PushPlus token 不能为空")
        if channel not in {"wechat", "app"}:
            raise ValueError("PushPlus channel 只能是 wechat 或 app")
        self._token = token
        self._channel = channel
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._now = now or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> PushPlusNotifier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, digest: Digest) -> SendResult:
        return self._send(digest, timeout_seconds=None)

    def send_with_timeout(self, digest: Digest, *, timeout_seconds: float) -> SendResult:
        if timeout_seconds <= 0:
            return SendResult(
                state=SendState.PENDING,
                error_kind=ErrorKind.TEMPORARY_NOT_SENT,
                error_message="没有剩余的通知请求时间预算",
            )
        return self._send(digest, timeout_seconds=timeout_seconds)

    def _send(self, digest: Digest, *, timeout_seconds: float | None) -> SendResult:
        # token 只放在 JSON body，绝不拼到 URL、日志或错误消息中。
        payload = {
            "token": self._token,
            "title": digest.title,
            "content": digest.content,
            "channel": self._channel,
            "template": "txt",
        }
        try:
            if timeout_seconds is None:
                response = self.client.post(self.ENDPOINT, json=payload)
            else:
                effective_timeout = min(timeout_seconds, self._timeout_seconds)
                response = self.client.post(
                    self.ENDPOINT,
                    json=payload,
                    timeout=httpx.Timeout(effective_timeout),
                )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError):
            return SendResult(
                state=SendState.PENDING,
                error_kind=ErrorKind.TEMPORARY_NOT_SENT,
                error_message="PushPlus 连接失败，未确认请求已送出",
                retry_at=self._now() + timedelta(minutes=30),
            )
        except (httpx.WriteError, httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.UNKNOWN,
                error_message="PushPlus 传输中断，结果未知，已暂停自动重发",
            )
        except httpx.HTTPError:
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.UNKNOWN,
                error_message="PushPlus 网络请求结果未知，已暂停自动重发",
            )

        if 300 <= response.status_code < 400:
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.CONTRACT,
                error_message="PushPlus 返回重定向，未跟随且结果未知",
            )
        if response.status_code == 429:
            retry_after = _retry_after(response.headers.get("retry-after"))
            return SendResult(
                state=SendState.PENDING,
                error_kind=ErrorKind.RATE_LIMIT,
                error_message="PushPlus 触发频率限制，服务端未确认受理",
                retry_at=self._now() + timedelta(seconds=max(1800, retry_after or 0)),
            )
        if 500 <= response.status_code <= 599:
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.UNKNOWN,
                error_message="PushPlus 服务端错误，结果未知，已暂停自动重发",
            )
        if response.status_code in {401, 403}:
            return SendResult(
                state=SendState.FAILED,
                error_kind=ErrorKind.AUTH,
                error_message="PushPlus 凭据或访问权限无效",
            )
        if response.status_code != 200:
            return SendResult(
                state=SendState.FAILED,
                error_kind=ErrorKind.PERMANENT,
                error_message=f"PushPlus 返回未预期 HTTP 状态 {response.status_code}",
            )

        try:
            if len(response.content) > 1024 * 1024:
                raise ValueError("响应过大")
            body = json.loads(response.content)
        except (json.JSONDecodeError, ValueError):
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.MALFORMED_SUCCESS,
                error_message="PushPlus 成功响应无法解析，结果未知",
            )
        if not isinstance(body, dict):
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.MALFORMED_SUCCESS,
                error_message="PushPlus 响应结构异常，结果未知",
            )
        code = body.get("code")
        if isinstance(code, bool) or not isinstance(code, int | float):
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.MALFORMED_SUCCESS,
                error_message="PushPlus 响应缺少业务 code，结果未知",
            )
        if int(code) != 200:
            return SendResult(
                state=SendState.FAILED,
                error_kind=_pushplus_error_kind(int(code)),
                error_message="PushPlus 明确拒绝本次消息",
            )
        receipt = _receipt(body.get("data"))
        if not receipt:
            return SendResult(
                state=SendState.UNKNOWN,
                error_kind=ErrorKind.MALFORMED_SUCCESS,
                error_message="PushPlus 成功响应缺少受理流水号，结果未知",
            )
        return SendResult(state=SendState.ACCEPTED, provider_receipt=receipt)


def _receipt(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:256]
    if isinstance(value, dict):
        for key in ("shortCode", "shortcode", "id"):
            result = value.get(key)
            if isinstance(result, str) and result.strip():
                return result.strip()[:256]
    return None


def _pushplus_error_kind(code: int) -> ErrorKind:
    if code in {401, 403, 902, 903, 904, 905, 906}:
        return ErrorKind.AUTH
    if code in {900, 901, 908, 909, 910}:
        return ErrorKind.QUOTA
    return ErrorKind.BUSINESS


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return None
