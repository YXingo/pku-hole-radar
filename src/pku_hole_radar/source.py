from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx

from .models import ErrorKind, Page, Post


class SourceError(RuntimeError):
    """树洞来源的结构化错误，不保存响应正文。"""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class Source(Protocol):
    def fetch_page(self, page_token: str | None, page_size: int) -> Page:
        """获取一个列表页；page_token 对调用方保持不透明。"""


class FixtureSource:
    """只读 JSON fixture 来源；绝不创建网络客户端。"""

    def __init__(self, pages: list[Page | SourceError]) -> None:
        if not pages:
            raise ValueError("fixture 至少需要一个 page")
        self._pages = pages
        self.calls: list[tuple[str | None, int]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> FixtureSource:
        fixture_path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceError(ErrorKind.CONTRACT, f"fixture 无法读取：{fixture_path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("pages"), list):
            raise SourceError(ErrorKind.CONTRACT, "fixture 根节点必须含 pages 数组")
        pages: list[Page | SourceError] = []
        for item in raw["pages"]:
            if not isinstance(item, dict):
                raise SourceError(ErrorKind.CONTRACT, "fixture page 必须是对象")
            if "error" in item:
                pages.append(_fixture_error(item["error"]))
                continue
            posts_raw = item.get("posts", [])
            if not isinstance(posts_raw, list):
                raise SourceError(ErrorKind.CONTRACT, "fixture posts 必须是数组")
            posts = [_post_from_fixture(post) for post in posts_raw]
            next_page = item.get("next_page")
            if next_page is not None and not isinstance(next_page, str):
                next_page = str(next_page)
            total = item.get("total")
            if total is not None and (isinstance(total, bool) or not isinstance(total, int)):
                raise SourceError(ErrorKind.CONTRACT, "fixture total 必须是整数")
            pages.append(
                Page(
                    posts=posts,
                    next_page=next_page,
                    exhausted=bool(item.get("exhausted", False)),
                    total=total,
                )
            )
        return cls(pages)

    def fetch_page(self, page_token: str | None, page_size: int) -> Page:
        self.calls.append((page_token, page_size))
        if page_token is None:
            index = 0
        else:
            try:
                index = int(page_token) - 1
            except ValueError as exc:
                raise SourceError(ErrorKind.CONTRACT, "fixture page token 无效") from exc
        if index < 0 or index >= len(self._pages):
            raise SourceError(ErrorKind.CONTRACT, "fixture page token 超出范围")
        result = self._pages[index]
        if isinstance(result, SourceError):
            raise result
        return result


class SequenceSource:
    """测试替身：按调用顺序返回 page 或抛出结构化来源错误。"""

    def __init__(self, responses: list[Page | SourceError]) -> None:
        self.responses = responses
        self.calls: list[tuple[str | None, int]] = []

    def fetch_page(self, page_token: str | None, page_size: int) -> Page:
        self.calls.append((page_token, page_size))
        if not self.responses:
            raise SourceError(ErrorKind.CONTRACT, "测试来源没有更多响应")
        response = self.responses.pop(0)
        if isinstance(response, SourceError):
            raise response
        return response


class LiveTreeholeSource:
    """北大树洞 V3 列表适配器。

    endpoint 和链接模板必须由本地配置提供，并且由 doctor/CLI 验证为树洞 HTTPS
    host；适配器不自动登录，也不跟随重定向。
    """

    def __init__(
        self,
        endpoint: str,
        post_url_template: str | None,
        *,
        token: str,
        uuid: str | None = None,
        xsrf_token: str | None = None,
        pku_token: str | None = None,
        session_cookie: str | None = None,
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 15,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "treehole.pku.edu.cn"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("树洞 endpoint 必须是 treehole.pku.edu.cn 的 HTTPS 地址")
        if post_url_template:
            _validate_link_template(post_url_template)
        if not token.strip():
            raise ValueError("树洞 token 不能为空")
        self.endpoint = endpoint
        self.post_url_template = post_url_template or ""
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._owns_client = client is None
        timeout = httpx.Timeout(
            timeout=read_timeout_seconds,
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
        )
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "userAgent": "pku_web",
        }
        if uuid:
            self._headers["uuid"] = uuid
        if xsrf_token:
            self._headers["x-xsrf-token"] = xsrf_token
        host = parsed.hostname
        if pku_token:
            self.client.cookies.set("pku_token", pku_token, domain=host, path="/")
        if xsrf_token:
            self.client.cookies.set("XSRF-TOKEN", xsrf_token, domain=host, path="/")
        if session_cookie:
            self.client.cookies.set("_session", session_cookie, domain=host, path="/")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> LiveTreeholeSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch_page(self, page_token: str | None, page_size: int) -> Page:
        return self._fetch_page(page_token, page_size, timeout_seconds=None)

    def fetch_page_with_timeout(
        self,
        page_token: str | None,
        page_size: int,
        *,
        timeout_seconds: float,
    ) -> Page:
        if timeout_seconds <= 0:
            raise SourceError(ErrorKind.TEMPORARY, "没有剩余的来源请求时间预算")
        return self._fetch_page(page_token, page_size, timeout_seconds=timeout_seconds)

    def _fetch_page(
        self,
        page_token: str | None,
        page_size: int,
        *,
        timeout_seconds: float | None,
    ) -> Page:
        page = _page_number(page_token)
        if page_size <= 0:
            raise ValueError("page_size 必须是正数")
        params = {
            "page": str(page),
            "limit": str(page_size),
            "comment_limit": "0",
            "comment_stream": "1",
        }
        try:
            if timeout_seconds is None:
                response = self.client.get(self.endpoint, params=params, headers=self._headers)
            else:
                effective_timeout = min(timeout_seconds, self._read_timeout_seconds)
                response = self.client.get(
                    self.endpoint,
                    params=params,
                    headers=self._headers,
                    timeout=httpx.Timeout(
                        effective_timeout,
                        connect=min(timeout_seconds, self._connect_timeout_seconds),
                        read=effective_timeout,
                        write=effective_timeout,
                        pool=min(timeout_seconds, self._connect_timeout_seconds),
                    ),
                )
        except httpx.ConnectTimeout as exc:
            raise SourceError(
                ErrorKind.TEMPORARY_NOT_SENT, "树洞连接超时，未确认请求已发出"
            ) from exc
        except (httpx.ConnectError, httpx.ProxyError) as exc:
            raise SourceError(ErrorKind.TEMPORARY_NOT_SENT, "树洞连接失败，请求未确认发出") from exc
        except httpx.ReadTimeout as exc:
            raise SourceError(ErrorKind.TEMPORARY, "树洞读取超时") from exc
        except httpx.WriteError as exc:
            raise SourceError(ErrorKind.TEMPORARY, "树洞请求写入中断") from exc
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            raise SourceError(ErrorKind.TEMPORARY, "树洞响应传输中断") from exc
        except httpx.HTTPError as exc:
            raise SourceError(ErrorKind.TEMPORARY, "树洞网络请求失败") from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            target = urlparse(location)
            if target.hostname and target.hostname != "treehole.pku.edu.cn":
                raise SourceError(ErrorKind.ACCESS_DENIED, "树洞请求发生跨域重定向")
            raise SourceError(ErrorKind.CONTRACT, "树洞列表不应返回重定向")
        if response.status_code == 401:
            raise SourceError(ErrorKind.AUTH, "树洞会话无效或已过期")
        if response.status_code == 403:
            raise SourceError(ErrorKind.ACCESS_DENIED, "树洞拒绝访问")
        if response.status_code == 429:
            raise SourceError(
                ErrorKind.RATE_LIMIT,
                "树洞请求触发频率限制",
                retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            )
        if 500 <= response.status_code <= 599:
            raise SourceError(ErrorKind.TEMPORARY, "树洞服务暂时不可用")
        if response.status_code != 200:
            raise SourceError(
                ErrorKind.CONTRACT, f"树洞返回未预期 HTTP 状态 {response.status_code}"
            )

        body = response.content
        if len(body) > 4 * 1024 * 1024:
            raise SourceError(ErrorKind.CONTRACT, "树洞响应超过本地大小上限")
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type or body.lstrip().startswith(b"<"):
            raise SourceError(ErrorKind.AUTH, "树洞返回 HTML 登录页而非 JSON")
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(ErrorKind.CONTRACT, "树洞响应不是有效 JSON") from exc
        if not isinstance(envelope, dict):
            raise SourceError(ErrorKind.CONTRACT, "树洞响应根节点不是对象")
        code = envelope.get("code")
        if isinstance(code, bool) or not isinstance(code, int | float):
            raise SourceError(ErrorKind.CONTRACT, "树洞响应缺少业务 code")
        if int(code) != 20000:
            if int(code) in {40002, 40008, 40010, 40077, 41411, 41511, 42411, 60001}:
                raise SourceError(ErrorKind.AUTH, "树洞会话或验证状态不可用")
            raise SourceError(ErrorKind.BUSINESS, "树洞业务响应拒绝请求")
        data = envelope.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise SourceError(ErrorKind.CONTRACT, "树洞响应缺少 data.list 数组")
        total = data.get("total")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int)):
            raise SourceError(ErrorKind.CONTRACT, "树洞 data.total 不是整数")
        posts = [_post_from_live(item, self.post_url_template) for item in data["list"]]
        exhausted = total is not None and page * page_size >= total
        return Page(
            posts=posts,
            next_page=None if exhausted else str(page + 1),
            exhausted=exhausted,
            total=total,
        )


def _post_from_fixture(raw: Any) -> Post:
    if not isinstance(raw, dict):
        raise SourceError(ErrorKind.CONTRACT, "fixture 帖子必须是对象")
    try:
        post_id = str(raw["id"])
        created_at = _parse_datetime(raw["created_at"])
        text = raw.get("text", "")
        url = str(raw.get("url", f"https://fixture.test/post/{post_id}"))
        return Post(
            id=post_id,
            created_at=created_at,
            text=text if isinstance(text, str) else str(text),
            url=url,
            is_pinned=bool(raw.get("is_pinned", False)),
            has_media=bool(raw.get("has_media", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(ErrorKind.CONTRACT, "fixture 帖子字段无效") from exc


def _post_from_live(raw: Any, link_template: str | None) -> Post:
    if not isinstance(raw, dict):
        raise SourceError(ErrorKind.CONTRACT, "树洞 data.list 帖子不是对象")
    pid = raw.get("pid")
    if isinstance(pid, bool) or pid is None:
        raise SourceError(ErrorKind.CONTRACT, "树洞帖子缺少 pid")
    post_id = str(pid)
    if not re.fullmatch(r"[0-9]+", post_id):
        raise SourceError(ErrorKind.CONTRACT, "树洞 pid 不是十进制 ID")
    timestamp = raw.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
        raise SourceError(ErrorKind.CONTRACT, "树洞帖子 timestamp 不是数字")
    try:
        created_at = datetime.fromtimestamp(float(timestamp), tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceError(ErrorKind.CONTRACT, "树洞帖子 timestamp 无法转换") from exc
    text = raw.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise SourceError(ErrorKind.CONTRACT, "树洞帖子 text 不是字符串")
    media_ids = raw.get("media_ids", "")
    has_media = bool(media_ids and str(media_ids) not in {"0", "[]"})
    url = (
        link_template.replace("{id}", quote(post_id, safe=""))
        if link_template
        else "https://treehole.pku.edu.cn/"
    )
    return Post(
        id=post_id,
        created_at=created_at,
        text=text,
        url=url,
        is_pinned=_truthy_flag(raw.get("is_top", 0)),
        has_media=has_media,
    )


def _fixture_error(raw: Any) -> SourceError:
    if not isinstance(raw, dict):
        return SourceError(ErrorKind.CONTRACT, "fixture error 必须是对象")
    try:
        kind = ErrorKind(str(raw.get("kind", ErrorKind.TEMPORARY)))
    except ValueError:
        kind = ErrorKind.CONTRACT
    retry_after = raw.get("retry_after_seconds")
    if isinstance(retry_after, bool) or not isinstance(retry_after, int):
        retry_after = None
    return SourceError(kind, "fixture 模拟来源错误", retry_after_seconds=retry_after)


def _parse_datetime(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("created_at 必须是 ISO 字符串")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _page_number(page_token: str | None) -> int:
    if page_token is None:
        return 1
    if not re.fullmatch(r"[1-9][0-9]*", page_token):
        raise SourceError(ErrorKind.CONTRACT, "树洞 page token 无效")
    return int(page_token)


def _validate_link_template(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "treehole.pku.edu.cn"
        or "{id}" not in value
        or parsed.username
        or parsed.password
    ):
        raise ValueError("帖子链接模板必须是树洞 HTTPS host 且含 {id}")


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes"}


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return None
