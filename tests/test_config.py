"""Tests for configuration loading, precedence, defaults, and redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_google_chat.config import (
    DEFAULT_LISTEN_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    Config,
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
