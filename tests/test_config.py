from __future__ import annotations

from pathlib import Path

import pytest

from pku_hole_radar.config import ConfigError, load_config, load_secrets


def write_config(path: Path, *, provider: str = "stdout", include: str = "[]") -> Path:
    path.write_text(
        f"""
[app]
timezone = "Asia/Shanghai"
state_dir = "state"
secrets_file = ".env"

[poll]
interval_seconds = 1800
page_size = 30
max_pages = 3
max_posts = 0
max_requests = 6
request_spacing_seconds = 2
retry_wait_seconds = 5
run_timeout_seconds = 180
connect_timeout_seconds = 5
read_timeout_seconds = 15

[filters]
include_any = {include}
exclude_any = []

[notify]
provider = "{provider}"
channel = "wechat"
content_mode = "snippet"
max_items = 20
snippet_chars = 120
max_message_chars = 6000
max_send_attempts_per_day = 60
pending_ttl_hours = 24
send_retry_spacing_seconds = 1800
""",
        encoding="utf-8",
    )
    return path


def test_config_resolves_paths_relative_to_config_file(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.toml"))
    assert config.app.state_dir == (tmp_path / "state").resolve()
    assert config.app.secrets_file == (tmp_path / ".env").resolve()
    assert config.poll.page_size == 30
    assert config.notify.provider == "stdout"


def test_blank_keyword_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="空白关键词"):
        load_config(write_config(tmp_path / "config.toml", include='[" "]'))


def test_zero_interval_and_unlimited_items_are_allowed(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.toml")
    content = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        content.replace("interval_seconds = 1800", "interval_seconds = 0")
        .replace("max_pages = 3", "max_pages = 0")
        .replace("max_posts = 0", "max_posts = 100")
        .replace('channel = "wechat"', 'channel = "app"')
        .replace("max_items = 20", "max_items = 0"),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.poll.interval_seconds == 0
    assert config.poll.max_pages == 0
    assert config.poll.max_posts == 100
    assert config.notify.channel == "app"
    assert config.notify.max_items == 0


def test_zero_request_limit_is_allowed(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.toml")
    content = config_path.read_text(encoding="utf-8").replace(
        "max_requests = 6", "max_requests = 0"
    )
    config_path.write_text(content, encoding="utf-8")

    config = load_config(config_path)

    assert config.poll.max_requests == 0


def test_environment_value_wins_without_overwriting(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.toml")
    (tmp_path / ".env").write_text("PUSHPLUS_TOKEN=file-value\n", encoding="utf-8")
    config = load_config(config_path)
    secrets = load_secrets(config, {"PUSHPLUS_TOKEN": "environment-value"})
    assert secrets["PUSHPLUS_TOKEN"] == "environment-value"


def test_only_known_secret_names_are_accepted(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.toml")
    (tmp_path / ".env").write_text("UNSUPPORTED=value\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="不支持的字段"):
        load_secrets(load_config(config_path), {})


def test_live_validation_rejects_world_readable_secret_file(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.toml")
    (tmp_path / ".env").write_text("PKUHOLE_TOKEN=local\n", encoding="utf-8")
    config = load_config(config_path)
    with pytest.raises(ConfigError, match="权限过宽"):
        config.validate_for("probe-source", {"PKUHOLE_TOKEN": "local"})


def test_probe_can_validate_endpoint_before_link_template_and_contract_confirmation(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path / "config.toml")
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[source]\nendpoint = "https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments"\n'
        + "contract_confirmed = false\n",
        encoding="utf-8",
    )
    secrets_path = tmp_path / ".env"
    secrets_path.write_text("PKUHOLE_TOKEN=local\n", encoding="utf-8")
    secrets_path.chmod(0o600)
    config = load_config(config_path)
    config.validate_for("probe-source", {"PKUHOLE_TOKEN": "local"})
    with pytest.raises(ConfigError, match="post_url_template"):
        config.validate_for("preview-live", {"PKUHOLE_TOKEN": "local"})
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "contract_confirmed = false\n",
            "contract_confirmed = false\n"
            'post_url_template = "https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid={id}"\n',
        ),
        encoding="utf-8",
    )
    confirmed_fields = load_config(config_path)
    with pytest.raises(ConfigError, match="contract_confirmed"):
        confirmed_fields.validate_for("preview-live", {"PKUHOLE_TOKEN": "local"})
