from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pku_hole_radar.cli import main
from pku_hole_radar.config import load_config
from pku_hole_radar.models import Coverage, Digest, Post
from pku_hole_radar.store import Store


def test_fixture_preview_uses_temporary_state_and_never_creates_production_db(
    tmp_path: Path, capsys
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        Path("config.example.toml")
        .read_text(encoding="utf-8")
        .replace('state_dir = "var"', 'state_dir = "production-state"')
        .replace('secrets_file = ".env"', 'secrets_file = "secrets.env"'),
        encoding="utf-8",
    )
    fixture = Path("tests/fixtures/basic.json").resolve()
    exit_code = main(["--config", str(config), "preview", "--fixture", str(fixture)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "合成 fixture" in output
    assert not (tmp_path / "production-state").exists()


def test_live_command_missing_configuration_returns_config_error_without_network(
    tmp_path: Path, capsys
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    exit_code = main(["--config", str(config), "run-once", "--source", "live"])
    error = capsys.readouterr().err
    assert exit_code == 2
    assert "缺少配置" in error
    assert "secret-value" not in error


def test_resume_source_can_explicitly_clear_cooldown(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        Path("config.example.toml")
        .read_text(encoding="utf-8")
        .replace('state_dir = "var"', 'state_dir = "state"')
        .replace('secrets_file = ".env"', 'secrets_file = "secrets.env"'),
        encoding="utf-8",
    )
    loaded = load_config(config)
    until = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat()
    with Store(loaded.app.database_path) as store:
        store.set_states(
            {
                "source_paused": "1",
                "pause_reason": "fixture",
                "cooldown_until": until,
                "cooldown_reason": "连续三轮临时采集失败",
                "consecutive_temp_failures": "3",
            }
        )

    assert main(["--config", str(config), "resume-source", "--clear-cooldown"]) == 0
    assert "保护冷却" in capsys.readouterr().out
    with Store(loaded.app.database_path) as store:
        assert store.get_state("source_paused") == "0"
        assert store.get_state("cooldown_until") is None
        assert store.get_state("cooldown_reason") is None
        assert store.get_state("consecutive_temp_failures") == "0"


def test_status_and_outbox_list_expose_batch_for_manual_recovery(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        Path("config.example.toml")
        .read_text(encoding="utf-8")
        .replace('state_dir = "var"', 'state_dir = "state"')
        .replace('secrets_file = ".env"', 'secrets_file = "secrets.env"'),
        encoding="utf-8",
    )
    loaded = load_config(config)
    now = datetime.now(UTC).replace(microsecond=0)
    batch_id = "batch-cli-unknown"
    with Store(loaded.app.database_path) as store:
        post = Post(
            id="201",
            created_at=now,
            text="manual recovery",
            url="https://fixture.test/post/201",
        )
        store.commit_collection(
            now=now,
            posts=[post],
            matched_ids={post.id},
            digest=Digest(
                batch_id=batch_id,
                title="待处理批次",
                content="不要在列表命令中展开正文",
                post_ids=(post.id,),
                created_at=now,
                coverage=Coverage.BOUNDED,
                post_count=1,
                shown_count=1,
            ),
            coverage=Coverage.BOUNDED,
            proposed_watermark=None,
        )
        day_start = now.replace(hour=0, minute=0, second=0)
        claim = store.claim_outbox(
            now=now,
            daily_limit=60,
            day_start=day_start,
            day_end=day_start + timedelta(days=1),
            pending_ttl=timedelta(days=1),
        ).claim
        assert claim is not None
        assert store.recover_sending(now + timedelta(seconds=1)) == 1

    assert main(["--config", str(config), "status"]) == 0
    status_output = capsys.readouterr().out
    assert batch_id in status_output
    assert "process_crash" in status_output

    assert (
        main(
            [
                "--config",
                str(config),
                "outbox",
                "list",
                "--status",
                "unknown",
            ]
        )
        == 0
    )
    list_output = capsys.readouterr().out
    assert batch_id in list_output
    assert "错误类型=process_crash" in list_output
    assert "不要在列表命令中展开正文" not in list_output

    assert (
        main(
            [
                "--config",
                str(config),
                "outbox",
                "retry",
                batch_id,
                "--ack-possible-duplicate",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with Store(loaded.app.database_path) as store:
        day_start = now.replace(hour=0, minute=0, second=0)
        claim = store.claim_outbox(
            now=now + timedelta(seconds=2),
            daily_limit=60,
            day_start=day_start,
            day_end=day_start + timedelta(days=1),
            pending_ttl=timedelta(days=1),
        ).claim
        assert claim is not None
        assert store.recover_sending(now + timedelta(seconds=3)) == 1

    assert main(["--config", str(config), "outbox", "discard", batch_id]) == 0
    discard_output = capsys.readouterr().out
    assert batch_id in discard_output
    with Store(loaded.app.database_path) as store:
        assert store.latest_outbox_status(batch_id).value == "discarded"
