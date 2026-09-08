from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic

import httpx
import pytest

from pku_hole_radar.models import Coverage, Digest, ErrorKind, SendState
from pku_hole_radar.notifier import PushPlusNotifier
from pku_hole_radar.source import LiveTreeholeSource, SourceError


def digest() -> Digest:
    return Digest(
        batch_id="batch",
        title="测试标题",
        content="测试正文",
        post_ids=("123",),
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
        coverage=Coverage.BOUNDED,
        post_count=1,
        shown_count=1,
    )


def live_source_with_client(client: httpx.Client) -> LiveTreeholeSource:
    return LiveTreeholeSource(
        "https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments",
        "https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid={id}",
        token="local-token",
        uuid="local-uuid",
        client=client,
    )


def live_source(handler) -> LiveTreeholeSource:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return live_source_with_client(client)


def test_live_adapter_maps_verified_shape_and_does_not_put_auth_in_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 20000,
                "data": {
                    "list": [
                        {
                            "pid": 123,
                            "text": "正文",
                            "timestamp": 1_757_020_800,
                            "is_top": 0,
                            "media_ids": "",
                        }
                    ],
                    "total": 25,
                },
            },
        )

    source = live_source(handler)
    try:
        page = source.fetch_page(None, 20)
    finally:
        source.close()
    assert page.next_page == "2"
    assert page.exhausted is False
    assert page.posts[0].id == "123"
    assert page.posts[0].url == "https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid=123"
    request = requests[0]
    assert request.url.params["page"] == "1"
    assert request.url.params["limit"] == "20"
    assert request.url.params["comment_limit"] == "0"
    assert request.url.params["comment_stream"] == "1"
    assert "local-token" not in str(request.url)
    assert request.headers["Authorization"] == "Bearer local-token"
    assert request.headers["uuid"] == "local-uuid"


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(401, text="Unauthorized"), ErrorKind.AUTH),
        (httpx.Response(403, text="Forbidden"), ErrorKind.ACCESS_DENIED),
        (httpx.Response(200, headers={"content-type": "text/html"}, text="login"), ErrorKind.AUTH),
        (httpx.Response(200, json={"code": 50000, "message": "denied"}), ErrorKind.BUSINESS),
    ],
)
def test_live_adapter_classifies_auth_html_and_business_failure(response, kind: ErrorKind) -> None:
    source = live_source(lambda _request: response)
    try:
        with pytest.raises(SourceError) as error:
            source.fetch_page(None, 20)
    finally:
        source.close()
    assert error.value.kind == kind
    assert "local-token" not in error.value.message


def test_live_adapter_retries_are_runner_responsibility_and_transport_error_is_temporary() -> None:
    source = live_source(lambda _request: (_ for _ in ()).throw(httpx.ReadError("network")))
    try:
        with pytest.raises(SourceError) as error:
            source.fetch_page(None, 20)
    finally:
        source.close()
    assert error.value.kind == ErrorKind.TEMPORARY


def test_live_adapter_timeout_cancels_slow_async_transport() -> None:
    cancelled: list[bool] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={"code": 20000, "data": {"list": [], "total": 0}})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    source = live_source_with_client(client)
    started = monotonic()
    try:
        with pytest.raises(SourceError) as error:
            source.fetch_page_with_timeout(None, 20, timeout_seconds=0.05)
    finally:
        source.close()
        client.close()

    assert monotonic() - started < 0.35
    assert error.value.kind == ErrorKind.TEMPORARY
    assert cancelled == [True]


def test_pushplus_success_records_acceptance_receipt_and_fixed_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": "short-code"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    notifier = PushPlusNotifier("push-secret", client=client)
    result = notifier.send(digest())
    notifier.close()
    assert result.state == SendState.ACCEPTED
    assert result.provider_receipt == "short-code"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://www.pushplus.plus/send"
    assert "push-secret" not in str(request.url)
    payload = json.loads(request.content)
    assert payload == {
        "token": "push-secret",
        "title": "测试标题",
        "content": "测试正文",
        "channel": "wechat",
        "template": "txt",
    }


def test_pushplus_can_use_app_device_channel() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": "app-short-code"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    notifier = PushPlusNotifier("push-secret", channel="app", client=client)
    result = notifier.send(digest())
    notifier.close()
    assert result.state == SendState.ACCEPTED
    payload = json.loads(requests[0].content)
    assert payload["channel"] == "app"
    assert payload["template"] == "txt"


@pytest.mark.parametrize(
    ("response", "state", "kind"),
    [
        (httpx.Response(200, json={"code": 900, "data": None}), SendState.FAILED, ErrorKind.QUOTA),
        (httpx.Response(200, json={"code": 200}), SendState.UNKNOWN, ErrorKind.MALFORMED_SUCCESS),
        (httpx.Response(500, text="server error"), SendState.UNKNOWN, ErrorKind.UNKNOWN),
        (
            httpx.Response(429, headers={"Retry-After": "3600"}),
            SendState.PENDING,
            ErrorKind.RATE_LIMIT,
        ),
    ],
)
def test_pushplus_does_not_turn_failure_or_unknown_into_acceptance(response, state, kind) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: response), follow_redirects=False
    )
    notifier = PushPlusNotifier("push-secret", client=client)
    result = notifier.send(digest())
    notifier.close()
    assert result.state == state
    assert result.error_kind == kind
    assert "push-secret" not in (result.error_message or "")


def test_pushplus_timeout_cancels_slow_async_transport_and_marks_result_unknown() -> None:
    cancelled: list[bool] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={"code": 200, "data": "short-code"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    notifier = PushPlusNotifier("push-secret", client=client)
    started = monotonic()
    try:
        result = notifier.send_with_timeout(digest(), timeout_seconds=0.05)
    finally:
        notifier.close()
        client.close()

    assert monotonic() - started < 0.35
    assert result.state == SendState.UNKNOWN
    assert result.error_kind == ErrorKind.UNKNOWN
    assert cancelled == [True]
