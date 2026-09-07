from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ConfigError, ensure_state_dir, file_mode, load_config, load_secrets
from .digest import DigestOptions
from .filtering import KeywordFilter
from .models import Coverage, Digest, ErrorKind, SendResult, SendState
from .notifier import PushPlusNotifier, StdoutNotifier
from .runner import LockBusy, ProcessLock, Runner, RunnerSettings, _day_bounds
from .source import FixtureSource, LiveTreeholeSource, SourceError
from .store import Store, StoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pku-hole-radar", description="北大树洞新帖本地提醒")
    parser.add_argument("--config", required=True, type=Path, help="配置文件绝对路径或相对路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="离线检查配置、权限和数据库")
    subparsers.add_parser("probe-source", help="请求树洞最新一页并报告契约信息")

    preview = subparsers.add_parser("preview", help="预览合成 fixture 或显式 live 结果")
    group = preview.add_mutually_exclusive_group(required=True)
    group.add_argument("--fixture", type=Path)
    group.add_argument("--live", action="store_true")

    run_once = subparsers.add_parser("run-once", help="执行一轮真实采集和投递")
    run_once.add_argument("--source", choices=["live"], required=True)
    subparsers.add_parser("notify-test", help="向配置的真实渠道发送合成测试消息")
    subparsers.add_parser("status", help="查看本地状态")

    posts = subparsers.add_parser("posts", help="查看批次中的本地帖子")
    posts.add_argument("--batch", required=True)

    outbox = subparsers.add_parser("outbox", help="处理待发送批次")
    outbox_sub = outbox.add_subparsers(dest="outbox_command", required=True)
    list_command = outbox_sub.add_parser("list", help="列出待处理和历史批次")
    list_command.add_argument(
        "--status",
        choices=[state.value for state in SendState],
        help="只显示指定状态",
    )
    list_command.add_argument(
        "--limit",
        type=_positive_cli_int,
        default=50,
        help="最多显示条数（默认 50）",
    )
    retry = outbox_sub.add_parser("retry")
    retry.add_argument("batch_id")
    retry.add_argument("--ack-possible-duplicate", action="store_true")
    discard = outbox_sub.add_parser("discard")
    discard.add_argument("batch_id")

    resume_source = subparsers.add_parser("resume-source", help="恢复来源采集")
    resume_source.add_argument(
        "--clear-cooldown",
        action="store_true",
        help="显式清除来源保护冷却及连续临时失败计数",
    )
    reset = subparsers.add_parser("baseline-reset", help="显式重设采集基线")
    reset.add_argument("--ack-skip-unseen", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        secrets = load_secrets(config)
        if args.command == "doctor":
            return _doctor(config, secrets)
        if args.command == "preview":
            return _preview(config, secrets, args)
        if args.command == "probe-source":
            return _probe_source(config, secrets)
        if args.command == "run-once":
            return _run_once(config, secrets)
        if args.command == "notify-test":
            return _notify_test(config, secrets)
        if args.command == "status":
            return _status(config)
        if args.command == "posts":
            return _posts(config, args.batch)
        if args.command == "outbox":
            return _outbox(config, args)
        if args.command == "resume-source":
            return _resume_source(config, clear_cooldown=args.clear_cooldown)
        if args.command == "baseline-reset":
            return _baseline_reset(config, secrets)
        parser.error(f"未知命令：{args.command}")
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except LockBusy as exc:
        print(f"未执行：{exc}", file=sys.stderr)
        return 6
    except StoreError as exc:
        print(f"本地状态错误：{exc}", file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"来源错误：{exc.message}", file=sys.stderr)
        return _source_exit_code(exc.kind)
    except OSError as exc:
        print(f"本地文件错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


def _doctor(config, secrets: dict[str, str]) -> int:
    ensure_state_dir(config)
    state_mode = file_mode(config.app.state_dir)
    secret_exists = config.app.secrets_file.is_file()
    secret_mode = file_mode(config.app.secrets_file) if secret_exists else None
    with Store(config.app.database_path) as store:
        schema_version = store.schema_version()
    print(f"配置：{config.path}")
    print(f"状态目录：{config.app.state_dir}（权限 {state_mode:04o}）")
    print(f"secrets 文件：{'存在' if secret_exists else '缺失'}")
    if secret_exists:
        print(f"secrets 权限：{secret_mode:04o}（要求无组/其他用户权限）")
    print(f"树洞会话：{'已提供' if secrets.get('PKUHOLE_TOKEN') else '未提供'}")
    print(f"PushPlus token：{'已提供' if secrets.get('PUSHPLUS_TOKEN') else '未提供'}")
    print(f"数据库：已初始化（schema {schema_version}）")
    if secret_exists and secret_mode is not None and secret_mode & 0o077:
        print("提示：请将 secrets 文件权限收紧为 0600；程序不会静默修改。", file=sys.stderr)
        return 2
    return 0


def _preview(config, secrets: dict[str, str], args: argparse.Namespace) -> int:
    if args.fixture is not None:
        source = FixtureSource.from_file(args.fixture)
        with tempfile.TemporaryDirectory(prefix="pku-hole-radar-preview-") as directory:
            store_path = Path(directory) / "preview.sqlite3"
            with Store(store_path) as store:
                # fixture preview 的语义是展示样本，不模拟首次启动的 baseline 静默轮次。
                store.set_states(
                    {"baseline_initialized": "1", "baseline_id": "0", "watermark_id": "0"}
                )
                runner = Runner(
                    store,
                    source,
                    notifier=StdoutNotifier(),
                    settings=_runner_settings(config, fixture=True),
                    keyword_filter=_keyword_filter(config),
                    allow_empty_baseline=True,
                )
                summary = runner.run_once()
                _print_summary(summary)
                return _summary_exit_code(summary, store)

    config.validate_for("preview-live", secrets)
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            source = _make_live_source(config, secrets)
            try:
                runner = Runner(
                    store,
                    source,
                    notifier=None,
                    settings=_runner_settings(config),
                    keyword_filter=_keyword_filter(config),
                    commit=False,
                    send=False,
                )
                summary = runner.run_once()
                if runner.last_digest is not None:
                    print(runner.last_digest.title)
                    print(runner.last_digest.content)
                elif runner.last_fetch and runner.last_fetch.baseline:
                    print("当前尚未建立生产基线；live preview 不会建立基线，因此本次不生成通知。")
                _print_summary(summary)
                return _summary_exit_code(summary, store)
            finally:
                source.close()


def _probe_source(config, secrets: dict[str, str]) -> int:
    config.validate_for("probe-source", secrets)
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            source = _make_live_source(config, secrets)
            try:
                runner = Runner(
                    store,
                    source,
                    notifier=None,
                    settings=_runner_settings(config),
                    keyword_filter=KeywordFilter(),
                    commit=False,
                    send=False,
                )
                summary = runner.run_once()
                fetch = runner.last_fetch
                if fetch is not None:
                    print(
                        f"来源探测：解析 {len(fetch.posts)} 个帖子，页数 {fetch.pages}，"
                        f"请求 {fetch.request_count}，覆盖 {fetch.coverage.value}"
                    )
                    ids = [int(post.id) for post in fetch.posts]
                    ordered = all(left >= right for left, right in zip(ids, ids[1:], strict=False))
                    pinned = sum(post.is_pinned for post in fetch.posts)
                    print(
                        f"PID（响应映射后）前 {min(10, len(ids))} 个：{ids[:10]}；"
                        f"当前样本非升序检查：{'通过' if ordered else '不通过'}；置顶数：{pinned}"
                    )
                print(
                    "当前映射字段：pid、text、timestamp(seconds)、is_top、media_ids；"
                    "未写入基线或 outbox。"
                )
                _print_summary(summary)
                return _summary_exit_code(summary, store)
            finally:
                source.close()


def _run_once(config, secrets: dict[str, str]) -> int:
    config.validate_for("run-once", secrets)
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            source = _make_live_source(config, secrets)
            notifier = _make_notifier(config, secrets)
            try:
                runner = Runner(
                    store,
                    source,
                    notifier=notifier,
                    settings=_runner_settings(config),
                    keyword_filter=_keyword_filter(config),
                )
                summary = runner.run_once()
                _print_summary(summary)
                return _summary_exit_code(summary, store)
            finally:
                source.close()
                _close_if_needed(notifier)


def _notify_test(config, secrets: dict[str, str]) -> int:
    config.validate_for("notify-test", secrets)
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            notifier = _make_notifier(config, secrets)
            now = datetime.now(UTC)
            day_start, day_end = _day_bounds(now, config.app.timezone)
            attempt_id = store.start_test_attempt(
                now=now,
                daily_limit=config.notify.max_send_attempts_per_day,
                day_start=day_start,
                day_end=day_end,
            )
            if attempt_id is None:
                cooldown_until = store.notification_cooldown_until()
                if cooldown_until is not None and cooldown_until > now:
                    print(
                        f"通知渠道仍在冷却期，最早可发送时间：{cooldown_until.isoformat()}。",
                        file=sys.stderr,
                    )
                else:
                    print("今日通知尝试预算已用尽。", file=sys.stderr)
                _close_if_needed(notifier)
                return 5
            digest = Digest(
                batch_id=f"test-{attempt_id[:12]}",
                title="PKUHoleRadar 测试消息",
                content="这是本地显式 notify-test 生成的合成消息。",
                post_ids=(),
                created_at=now,
                coverage=Coverage.BASELINE,
                post_count=0,
                shown_count=0,
            )
            try:
                result = notifier.send(digest)
            except Exception:
                result = SendResult(
                    state=SendState.UNKNOWN,
                    error_kind=ErrorKind.UNKNOWN,
                    error_message="通知请求结果未知，已暂停自动重发",
                )
            store.finish_send(attempt_id, datetime.now(UTC), result)
            _close_if_needed(notifier)
            print(f"测试通知状态：{result.state.value}")
            if result.provider_receipt:
                print(f"服务端受理流水号：{result.provider_receipt}")
            if result.error_message:
                print(f"结果说明：{result.error_message}")
            return 0 if result.state in {SendState.ACCEPTED, SendState.DELIVERED} else 5


def _status(config) -> int:
    ensure_state_dir(config)
    with Store(config.app.database_path) as store:
        values = store.all_state()
        counts = store.outbox_counts()
        attention_rows = [
            row
            for status in (
                SendState.PENDING,
                SendState.SENDING,
                SendState.UNKNOWN,
                SendState.FAILED,
            )
            for row in store.list_outbox(status=status, limit=1000)
        ]
        now = datetime.now(UTC)
        day_start, day_end = _day_bounds(now, config.app.timezone)
        attempts = store.send_attempt_count(day_start, day_end)
    print(f"基线：{'已建立' if values.get('baseline_initialized') == '1' else '未建立'}")
    print(f"水位：{values.get('watermark_id') or '无上界/未建立'}")
    print(f"覆盖：{values.get('coverage') or '未知'}")
    print(f"上次成功：{values.get('last_success_at') or '无'}")
    print(f"下次采集：{values.get('next_poll_at') or '未安排'}")
    print(f"冷却至：{values.get('cooldown_until') or '无'}")
    print(f"通知渠道冷却至：{values.get('notify_cooldown_until') or '无'}")
    if values.get("notify_cooldown_reason"):
        print(f"通知冷却原因：{values['notify_cooldown_reason']}")
    print(f"来源暂停：{'是' if values.get('source_paused') == '1' else '否'}")
    if values.get("pause_reason"):
        print(f"暂停原因：{values['pause_reason']}")
    print(f"outbox：{', '.join(f'{key}={value}' for key, value in sorted(counts.items())) or '空'}")
    print(f"今日发送尝试：{attempts}/{config.notify.max_send_attempts_per_day}")
    if attention_rows:
        print("需关注批次（完整详情请执行 outbox list）：")
        for row in attention_rows:
            print(_format_outbox_row(row))
    return 0


def _posts(config, batch_id: str) -> int:
    with Store(config.app.database_path) as store:
        rows = store.posts_for_batch(batch_id)
    if not rows:
        print("批次不存在或已清理帖子关联。")
        return 0
    for row in rows:
        print(
            f"#{row['id']} {row['created_at']} matched={row['matched']} "
            f"{row['snippet']}\n{row['url']}"
        )
    return 0


def _outbox(config, args: argparse.Namespace) -> int:
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            if args.outbox_command == "list":
                rows = store.list_outbox(status=args.status, limit=args.limit)
                if not rows:
                    print("没有符合条件的 outbox 批次。")
                    return 0
                for row in rows:
                    print(_format_outbox_row(row))
                return 0
            now = datetime.now(UTC)
            if args.outbox_command == "retry":
                store.retry_outbox(
                    args.batch_id,
                    now,
                    acknowledge_duplicate=args.ack_possible_duplicate,
                )
                print(f"批次已重新排队：{args.batch_id}")
                return 0
            store.discard_outbox(args.batch_id, now)
            print(f"批次已标记 discarded：{args.batch_id}")
            return 0


def _resume_source(config, *, clear_cooldown: bool = False) -> int:
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            values: dict[str, str | None] = {"source_paused": "0", "pause_reason": None}
            if clear_cooldown:
                values.update(
                    {
                        "cooldown_until": None,
                        "cooldown_reason": None,
                        "consecutive_temp_failures": "0",
                    }
                )
            store.set_states(values)
    if clear_cooldown:
        print("已清除来源认证暂停状态和保护冷却；基线与去重状态保留。")
    else:
        print("已清除来源认证暂停状态；已有 cooldown 和基线均保留。")
    return 0


def _baseline_reset(config, secrets: dict[str, str]) -> int:
    config.validate_for("baseline-reset", secrets)
    ensure_state_dir(config)
    with ProcessLock(config.app.lock_path):
        _configure_logging(config)
        with Store(config.app.database_path) as store:
            source = _make_live_source(config, secrets)
            runner: Runner | None = None
            try:
                now = datetime.now(UTC)
                next_poll = _parse_time(store.get_state("next_poll_at"))
                cooldown = _parse_time(store.get_state("cooldown_until"))
                if store.get_state("source_paused") == "1":
                    print("来源处于暂停状态，请先执行 resume-source。", file=sys.stderr)
                    return 3
                if config.poll.interval_seconds > 0 and next_poll and now < next_poll:
                    print(
                        f"仍在采集间隔内，最早可执行时间：{store.get_state('next_poll_at')}",
                        file=sys.stderr,
                    )
                    return 4
                if cooldown and now < cooldown:
                    print(
                        f"仍在来源冷却期，最早可执行时间：{store.get_state('cooldown_until')}",
                        file=sys.stderr,
                    )
                    return 4
                next_poll_at = (
                    now + timedelta(seconds=config.poll.interval_seconds)
                    if config.poll.interval_seconds > 0
                    else None
                )
                store.set_states(
                    {
                        "last_poll_started_at": _iso(now),
                        "next_poll_at": _iso(next_poll_at) if next_poll_at else None,
                    }
                )
                runner = Runner(
                    store,
                    source,
                    notifier=None,
                    settings=_runner_settings(config),
                    commit=False,
                    send=False,
                )
                page, request_count = runner.fetch_latest_page()
                ordinary = [post for post in page.posts if not post.is_pinned]
                if not ordinary:
                    raise SourceError(ErrorKind.CONTRACT, "最新页没有可确认的普通帖，未重设基线")
                watermark = max(ordinary, key=lambda post: int(post.id)).id
                inserted = store.reset_baseline(now=now, posts=page.posts, watermark=watermark)
                store.set_states(
                    {
                        "baseline_reset_note": "用户确认跳过未观测历史",
                        "last_success_at": _iso(datetime.now(UTC)),
                        "last_poll_finished_at": _iso(datetime.now(UTC)),
                        "last_error_kind": None,
                        "last_error_message": None,
                    }
                )
                print(
                    f"基线已重设：已记录 {inserted} 个帖子，已知最大普通帖 ID={watermark}，"
                    f"请求 {request_count} 次。"
                )
                return 0
            except SourceError as exc:
                if runner is not None:
                    runner.record_source_error(exc, datetime.now(UTC))
                else:
                    store.set_states(
                        {
                            "last_error_kind": exc.kind.value,
                            "last_error_message": exc.message,
                            "last_poll_finished_at": _iso(datetime.now(UTC)),
                        }
                    )
                print(f"基线重设失败：{exc.message}", file=sys.stderr)
                return _source_exit_code(exc.kind)
            finally:
                source.close()


def _make_live_source(config, secrets: dict[str, str]) -> LiveTreeholeSource:
    return LiveTreeholeSource(
        config.source.endpoint,
        config.source.post_url_template,
        token=secrets["PKUHOLE_TOKEN"],
        uuid=secrets.get("PKUHOLE_UUID"),
        xsrf_token=secrets.get("PKUHOLE_XSRF_TOKEN"),
        pku_token=secrets.get("PKUHOLE_COOKIE_PKU_TOKEN"),
        session_cookie=secrets.get("PKUHOLE_SESSION_COOKIE"),
        connect_timeout_seconds=config.poll.connect_timeout_seconds,
        read_timeout_seconds=config.poll.read_timeout_seconds,
    )


def _make_notifier(config, secrets: dict[str, str]):
    if config.notify.provider == "stdout":
        return StdoutNotifier()
    return PushPlusNotifier(
        secrets["PUSHPLUS_TOKEN"],
        channel=config.notify.channel,
        timeout_seconds=config.poll.read_timeout_seconds,
    )


def _runner_settings(config, *, fixture: bool = False) -> RunnerSettings:
    options = DigestOptions(
        timezone=config.app.timezone,
        content_mode=config.notify.content_mode,
        max_items=config.notify.max_items,
        snippet_chars=config.notify.snippet_chars,
        max_message_chars=config.notify.max_message_chars,
        url_template=None if fixture else config.source.post_url_template or None,
        allowed_hosts=frozenset({"fixture.test"} if fixture else {"treehole.pku.edu.cn"}),
    )
    return RunnerSettings(
        timezone=config.app.timezone,
        interval_seconds=config.poll.interval_seconds,
        page_size=config.poll.page_size,
        max_pages=config.poll.max_pages,
        max_posts=config.poll.max_posts,
        max_requests=config.poll.max_requests,
        request_spacing_seconds=config.poll.request_spacing_seconds,
        retry_wait_seconds=config.poll.retry_wait_seconds,
        run_timeout_seconds=config.poll.run_timeout_seconds,
        daily_send_limit=config.notify.max_send_attempts_per_day,
        pending_ttl_hours=config.notify.pending_ttl_hours,
        send_retry_spacing_seconds=config.notify.send_retry_spacing_seconds,
        cleanup_interval_seconds=86400,
        cleanup_batch_size=500,
        digest_options=options,
    )


def _keyword_filter(config) -> KeywordFilter:
    return KeywordFilter(
        include_any=config.filters.include_any,
        exclude_any=config.filters.exclude_any,
    )


def _print_summary(summary) -> None:
    coverage = summary.coverage.value if summary.coverage else "无"
    batch_state = summary.batch_state.value if summary.batch_state else "无"
    send_state = summary.send_state.value if summary.send_state else "无"
    print(
        f"轮次 {summary.run_id}：页 {summary.pages}，请求 {summary.request_count}，"
        f"新增 {summary.new_count}，匹配 {summary.matched_count}，覆盖 {coverage}，"
        f"批次 {batch_state}，发送 {send_state}，耗时 {summary.elapsed_seconds:.1f}s"
    )
    logging.getLogger("pku_hole_radar").info(
        "run_id=%s pages=%s requests=%s new=%s matched=%s coverage=%s batch_state=%s send_state=%s",
        summary.run_id,
        summary.pages,
        summary.request_count,
        summary.new_count,
        summary.matched_count,
        coverage,
        batch_state,
        send_state,
    )
    if summary.error_message:
        print(f"结果说明：{summary.error_message}", file=sys.stderr)


def _format_outbox_row(row) -> str:
    return (
        f"批次 {row['batch_id']}：状态={row['status']}，尝试={row['attempts']}，"
        f"下次发送={row['next_attempt_at'] or '无'}，"
        f"错误类型={row['last_error_kind'] or '无'}，"
        f"错误说明={row['last_error_message'] or '无'}，"
        f"创建={row['created_at']}，条数={row['post_count']}，展示={row['shown_count']}"
    )


def _summary_exit_code(summary, store: Store) -> int:
    if summary.error_kind in {ErrorKind.AUTH, ErrorKind.ACCESS_DENIED}:
        if store.get_state("source_paused") == "1":
            return 3
    if summary.coverage == Coverage.INCOMPLETE or summary.error_kind in {
        ErrorKind.TEMPORARY,
        ErrorKind.TEMPORARY_NOT_SENT,
        ErrorKind.RATE_LIMIT,
        ErrorKind.CONTRACT,
    }:
        return 4
    if summary.send_state in {SendState.PENDING, SendState.UNKNOWN, SendState.FAILED}:
        return 5
    return 0


def _source_exit_code(kind: ErrorKind) -> int:
    if kind in {ErrorKind.AUTH, ErrorKind.ACCESS_DENIED}:
        return 3
    if kind == ErrorKind.CONFIG:
        return 2
    return 4


def _close_if_needed(value: object) -> None:
    close = getattr(value, "close", None)
    if close is not None:
        close()


def _positive_cli_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _parse_time(value: str | None) -> datetime | None:
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


def _configure_logging(config) -> None:
    logger = logging.getLogger("pku_hole_radar")
    logger.setLevel(logging.INFO)
    target = str(config.app.log_path)
    for handler in list(logger.handlers):
        if getattr(handler, "baseFilename", None) == target:
            return
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        target,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
