from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # Python 3.11+ 标准库；保留 tomli 仅用于本机旧 Python 的离线开发。
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅在 Python 3.10 及更早版本触发
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """用户配置不可用，错误消息不包含秘密值。"""


SECRET_NAMES = (
    "PKUHOLE_TOKEN",
    "PKUHOLE_UUID",
    "PKUHOLE_XSRF_TOKEN",
    "PKUHOLE_COOKIE_PKU_TOKEN",
    "PKUHOLE_SESSION_COOKIE",
    "PUSHPLUS_TOKEN",
)
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AppSettings:
    timezone_name: str
    timezone: ZoneInfo
    state_dir: Path
    secrets_file: Path

    @property
    def database_path(self) -> Path:
        return self.state_dir / "pku-hole-radar.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "run.lock"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "pku-hole-radar.log"


@dataclass(frozen=True, slots=True)
class PollSettings:
    interval_seconds: int
    page_size: int
    max_pages: int
    max_posts: int
    max_requests: int
    request_spacing_seconds: float
    retry_wait_seconds: float
    run_timeout_seconds: float
    connect_timeout_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SourceSettings:
    endpoint: str
    post_url_template: str
    contract_confirmed: bool
    timestamp_unit: str


@dataclass(frozen=True, slots=True)
class FilterSettings:
    include_any: tuple[str, ...]
    exclude_any: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotifySettings:
    provider: str
    channel: str
    content_mode: str
    max_items: int
    snippet_chars: int
    max_message_chars: int
    max_send_attempts_per_day: int
    pending_ttl_hours: float
    send_retry_spacing_seconds: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    app: AppSettings
    poll: PollSettings
    source: SourceSettings
    filters: FilterSettings
    notify: NotifySettings

    def validate_for(self, command: str, secrets: Mapping[str, str]) -> None:
        if command in {"run-once", "probe-source", "preview-live", "baseline-reset"}:
            self._validate_source(secrets, require_contract=command != "probe-source")
        if command in {"run-once", "notify-test"}:
            if self.notify.provider == "stdout" and command == "run-once":
                raise ConfigError(
                    "run-once 必须使用已配置的真实通知渠道；请先用 preview 预览，"
                    "或将 notify.provider 设为 pushplus 并填写 PUSHPLUS_TOKEN"
                )
            self._validate_notifier(secrets)

    def _validate_source(
        self, secrets: Mapping[str, str], *, require_contract: bool = True
    ) -> None:
        self._validate_secrets_file_mode()
        missing = []
        if not self.source.endpoint:
            missing.append("source.endpoint")
        if require_contract and not self.source.post_url_template:
            missing.append("source.post_url_template")
        if not str(secrets.get("PKUHOLE_TOKEN", "")).strip():
            missing.append("PKUHOLE_TOKEN（本地 secrets 文件）")
        if missing:
            raise ConfigError("真实树洞操作缺少配置：" + ", ".join(missing))
        if require_contract and not self.source.contract_confirmed:
            raise ConfigError(
                "真实树洞契约尚未确认；完成最小 probe 后，在本地配置中将 "
                "source.contract_confirmed 设为 true"
            )

    def _validate_notifier(self, secrets: Mapping[str, str]) -> None:
        self._validate_secrets_file_mode()
        if self.notify.provider != "pushplus":
            raise ConfigError(f"不支持的通知渠道：{self.notify.provider}")
        if not str(secrets.get("PUSHPLUS_TOKEN", "")).strip():
            raise ConfigError("PushPlus 渠道缺少 PUSHPLUS_TOKEN（本地 secrets 文件）")

    def _validate_secrets_file_mode(self) -> None:
        mode = file_mode(self.app.secrets_file)
        if mode is not None and mode & 0o077:
            raise ConfigError(f"secrets_file 权限过宽：{self.app.secrets_file}；请在本地设为 0600")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"配置文件无法读取：{config_path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 TOML 表")
    base_dir = config_path.parent
    app_raw = _table(raw, "app")
    timezone_name = _string(app_raw, "timezone", "Asia/Shanghai")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"未知时区：{timezone_name}") from exc
    state_dir = _resolve_path(base_dir, _string(app_raw, "state_dir", "var"))
    secrets_file = _resolve_path(base_dir, _string(app_raw, "secrets_file", ".env"))

    poll_raw = _table(raw, "poll")
    poll = PollSettings(
        interval_seconds=_nonnegative_int(poll_raw, "interval_seconds", 1800),
        page_size=_positive_int(poll_raw, "page_size", 30),
        # 0 表示不按页数截断；仍受整轮运行看门狗保护。
        max_pages=_nonnegative_int(poll_raw, "max_pages", 3),
        # 0 表示不限制本轮新帖候选数；仍受整轮运行看门狗保护。
        max_posts=_nonnegative_int(poll_raw, "max_posts", 0),
        # 0 表示不设置单轮请求次数上限；仍受请求间隔和整轮运行看门狗保护。
        max_requests=_nonnegative_int(poll_raw, "max_requests", 0),
        request_spacing_seconds=_nonnegative_float(poll_raw, "request_spacing_seconds", 2),
        retry_wait_seconds=_nonnegative_float(poll_raw, "retry_wait_seconds", 5),
        run_timeout_seconds=_positive_float(poll_raw, "run_timeout_seconds", 180),
        connect_timeout_seconds=_positive_float(poll_raw, "connect_timeout_seconds", 5),
        read_timeout_seconds=_positive_float(poll_raw, "read_timeout_seconds", 15),
    )

    source_raw = _table(raw, "source")
    source = SourceSettings(
        endpoint=_string(source_raw, "endpoint", "").strip(),
        post_url_template=_string(source_raw, "post_url_template", "").strip(),
        contract_confirmed=_bool(source_raw, "contract_confirmed", False),
        timestamp_unit=_string(source_raw, "timestamp_unit", "seconds").strip().lower(),
    )
    _validate_source_values(source)

    filters_raw = _table(raw, "filters")
    filters = FilterSettings(
        include_any=_keywords(filters_raw, "include_any"),
        exclude_any=_keywords(filters_raw, "exclude_any"),
    )

    notify_raw = _table(raw, "notify")
    notify = NotifySettings(
        provider=_string(notify_raw, "provider", "stdout").strip().lower(),
        channel=_string(notify_raw, "channel", "wechat").strip().lower(),
        content_mode=_string(notify_raw, "content_mode", "snippet").strip().lower(),
        # 0 表示不按条数截断；仍受 max_message_chars 和渠道限制约束。
        max_items=_nonnegative_int(notify_raw, "max_items", 20),
        snippet_chars=_positive_int(notify_raw, "snippet_chars", 120),
        max_message_chars=_positive_int(notify_raw, "max_message_chars", 6000),
        max_send_attempts_per_day=_positive_int(notify_raw, "max_send_attempts_per_day", 60),
        pending_ttl_hours=_positive_float(notify_raw, "pending_ttl_hours", 24),
        send_retry_spacing_seconds=_nonnegative_float(
            notify_raw, "send_retry_spacing_seconds", 1800
        ),
    )
    if notify.provider not in {"stdout", "pushplus"}:
        raise ConfigError("notify.provider 只能是 stdout 或 pushplus")
    if notify.channel not in {"wechat", "app"}:
        raise ConfigError("notify.channel 只能是 wechat 或 app")
    if notify.content_mode not in {"snippet", "links_only"}:
        raise ConfigError("notify.content_mode 只能是 snippet 或 links_only")
    if notify.max_message_chars < 200:
        raise ConfigError("notify.max_message_chars 不能小于 200")

    return AppConfig(
        path=config_path,
        app=AppSettings(
            timezone_name=timezone_name,
            timezone=timezone,
            state_dir=state_dir,
            secrets_file=secrets_file,
        ),
        poll=poll,
        source=source,
        filters=filters,
        notify=notify,
    )


def load_secrets(config: AppConfig, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """只读取显式 secrets_file；已有环境变量优先且不会被文件覆盖。"""

    environment = os.environ if environ is None else environ
    values = {name: environment[name] for name in SECRET_NAMES if name in environment}
    path = config.app.secrets_file
    if not path.exists():
        return values
    if not path.is_file():
        raise ConfigError(f"secrets_file 不是普通文件：{path}")
    try:
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ConfigError(f"secrets_file 第 {line_number} 行缺少 =")
            key, value = (part.strip() for part in line.split("=", 1))
            if key not in SECRET_NAMES or not _ENV_KEY.fullmatch(key):
                raise ConfigError(f"secrets_file 第 {line_number} 行包含不支持的字段")
            values.setdefault(key, _unquote(value.strip()))
    except UnicodeError as exc:
        raise ConfigError(f"secrets_file 不是 UTF-8 文件：{path}") from exc
    return values


def ensure_state_dir(config: AppConfig) -> None:
    path = config.app.state_dir
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created:
        path.chmod(0o700)


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def mode_is_private(path: Path, expected: int) -> bool:
    mode = file_mode(path)
    return mode is not None and mode & 0o077 == 0 and mode & 0o700 == expected


def _validate_source_values(source: SourceSettings) -> None:
    if source.timestamp_unit != "seconds":
        raise ConfigError("source.timestamp_unit 当前只能是 seconds")
    for field_name, value in (
        ("source.endpoint", source.endpoint),
        ("source.post_url_template", source.post_url_template),
    ):
        if not value:
            continue
        is_link_template = field_name.endswith("post_url_template")
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "treehole.pku.edu.cn"
            or parsed.username
            or parsed.password
            or (not is_link_template and (parsed.query or parsed.fragment))
        ):
            raise ConfigError(f"{field_name} 必须是 treehole.pku.edu.cn 上的 HTTPS 地址")
        if is_link_template:
            if "{id}" not in value:
                raise ConfigError("source.post_url_template 必须含 {id}")


def _table(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    return value


def _string(raw: Mapping[str, Any], name: str, default: str) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} 必须是字符串")
    return value


def _bool(raw: Mapping[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} 必须是布尔值")
    return value


def _nonnegative_int(raw: Mapping[str, Any], name: str, default: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name} 必须是非负整数")
    return value


def _positive_int(raw: Mapping[str, Any], name: str, default: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} 必须是正整数")
    return value


def _positive_float(raw: Mapping[str, Any], name: str, default: float) -> float:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{name} 必须是正数")
    return float(value)


def _nonnegative_float(raw: Mapping[str, Any], name: str, default: float) -> float:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{name} 必须是非负数")
    return float(value)


def _keywords(raw: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"filters.{name} 必须是字符串数组")
    result = tuple(item for item in value if item.strip())
    if len(result) != len(value):
        raise ConfigError(f"filters.{name} 不允许空白关键词")
    return result


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
