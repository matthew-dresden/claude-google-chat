"""Tests for configuration loading, precedence, defaults, and redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_google_chat.config import (
    DEFAULT_LISTEN_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    Config,
    merge_and_write_config,
    merge_config_values,
)
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX


def test_load_from_toml_file(data_dir: Path) -> None:
    config = Config.load(path=data_dir / "config_valid.toml", env={})
    assert config.webhook_url is not None
    assert config.space_id == "spaces/AAAA"
    assert config.trigger_prefix == "bot-command:"
    assert isinstance(config.poll_interval, float)
    assert config.poll_interval == 5.0
    assert config.listen_timeout == 30.0


def test_env_overrides_file(data_dir: Path) -> None:
    env = {"CGC_TRIGGER_PREFIX": "env-command:"}
    config = Config.load(path=data_dir / "config_valid.toml", env=env)
    assert config.trigger_prefix == "env-command:"


def test_missing_required_raises(data_dir: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        Config.load(
            path=data_dir / "config_missing_space.toml",
            env={},
            require=("space_id",),
        )
    assert "space_id" in str(exc_info.value)


def test_defaults_applied(tmp_path: Path) -> None:
    minimal = tmp_path / "minimal.toml"
    minimal.write_text('webhook_url = "https://example/x"\n', encoding="utf-8")
    config = Config.load(path=minimal, env={})
    assert config.trigger_prefix == DEFAULT_TRIGGER_PREFIX
    assert config.poll_interval == DEFAULT_POLL_INTERVAL
    assert config.listen_timeout == DEFAULT_LISTEN_TIMEOUT


def test_show_masks_secrets(data_dir: Path) -> None:
    config = Config.load(path=data_dir / "config_valid.toml", env={})
    redacted = config.redacted()
    assert "SECRETKEY" not in str(redacted["webhook_url"])
    assert "SECRETTOKEN" not in str(redacted["webhook_url"])
    # The full URL must never appear verbatim.
    assert redacted["webhook_url"] != config.webhook_url


def test_merge_overwrites_and_preserves() -> None:
    existing = {"trigger_prefix": "claude-command:", "space_id": "spaces/OLD"}
    updates = {"space_id": "spaces/NEW", "pubsub_topic": "projects/p/topics/t"}
    merged = merge_config_values(existing, updates)
    assert merged["space_id"] == "spaces/NEW"
    assert merged["trigger_prefix"] == "claude-command:"
    assert merged["pubsub_topic"] == "projects/p/topics/t"


def test_merge_skips_none_updates() -> None:
    existing = {"space_id": "spaces/KEEP"}
    merged = merge_config_values(existing, {"space_id": None, "project_id": "p1"})
    assert merged["space_id"] == "spaces/KEEP"
    assert merged["project_id"] == "p1"


def test_merge_rejects_unknown_key() -> None:
    with pytest.raises(ValueError):
        merge_config_values({}, {"bogus_key": "x"})


def test_merge_and_write_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    merge_and_write_config({"space_id": "spaces/A", "trigger_prefix": "p:"}, path=path)
    merge_and_write_config({"space_id": "spaces/B"}, path=path)
    reloaded = Config.load(path=path, env={})
    assert reloaded.space_id == "spaces/B"
    assert reloaded.trigger_prefix == "p:"


def test_service_account_redacted() -> None:
    config = Config(service_account_file="/secret/path/key.json")
    redacted = config.redacted()
    assert redacted["service_account_file"] != "/secret/path/key.json"
