"""Tests for the Typer CLI (``cgc`` console script).

Every command is driven through :class:`typer.testing.CliRunner`, which invokes
the real Click/Typer argument parsing, callbacks, and exit-code handling. All
side-effecting collaborators are mocked at their import site so no network,
Google API, OAuth, or real OS-config-dir I/O occurs:

- ``default_config_path`` is redirected to a per-test ``tmp_path`` file so
  ``config init|set``, ``setup`` and ``Config.load`` never touch the real config
  directory.
- The lazily-imported workers (``auth.login``, ``chat.send_webhook``,
  ``listener.run``, ``chat.list_messages`` / ``chat.delete_message``) are patched
  so each command test asserts on the call it delegates and the exit code it
  surfaces.

Assertions check exit codes, emitted output, and the arguments forwarded to the
mocked workers; bad-input and error paths assert non-zero exit codes.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from claude_google_chat import __version__, cli
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, ChatMessage

WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=TEST_KEY&token=TEST_TOKEN"
SPACE_ID = "spaces/AAAA"


@pytest.fixture
def runner() -> CliRunner:
    """A Click test runner that captures stdout and stderr separately."""
    return CliRunner()


@pytest.fixture
def cli_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``default_config_path`` to a temp file in both call sites.

    ``cli.py`` imports ``default_config_path`` directly, while ``config.py`` uses
    its module-local reference inside ``Config.load`` / ``write_config``. Both
    are patched so the CLI never reads or writes the real OS config directory.
    """
    path = tmp_path / "cgc" / "config.toml"

    def _fake_path() -> Path:
        return path

    monkeypatch.setattr(cli, "default_config_path", _fake_path)
    monkeypatch.setattr("claude_google_chat.config.default_config_path", _fake_path)
    return path


@pytest.fixture
def write_cli_config(cli_config_path: Path) -> Callable[..., Path]:
    """Factory writing a ``config.toml`` at the patched default path."""

    def _toml_literal(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, (list, tuple)):
            inner = ", ".join(
                '"{}"'.format(str(e).replace("\\", "\\\\").replace('"', '\\"')) for e in value
            )
            return f"[{inner}]"
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _write(**values: object) -> Path:
        cli_config_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k} = {_toml_literal(v)}" for k, v in values.items() if v is not None]
        cli_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cli_config_path

    return _write


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip CGC_* env overrides so file-driven config is deterministic."""
    from claude_google_chat.config import ENV_OVERRIDES

    for env_var in ENV_OVERRIDES.values():
        monkeypatch.delenv(env_var, raising=False)


# --------------------------------------------------------------------------- #
# Top-level: --help / --version / no-args.
# --------------------------------------------------------------------------- #


def test_version_flag_prints_version_and_exits_zero(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("config", "auth", "chat", "listen", "clear", "status"):
        assert command in result.stdout


def test_no_args_shows_help_and_exits_nonzero(runner: CliRunner) -> None:
    # no_args_is_help=True -> Typer exits with code 2 and prints usage.
    result = runner.invoke(cli.app, [])
    assert result.exit_code != 0
    assert "Usage" in result.stdout


def test_unknown_command_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["frobnicate"])
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# config init / show / set.
# --------------------------------------------------------------------------- #


def test_config_init_creates_file(runner: CliRunner, cli_config_path: Path) -> None:
    assert not cli_config_path.exists()
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0
    assert "created config" in result.stdout
    assert cli_config_path.exists()


def test_config_init_is_idempotent_when_present(
    runner: CliRunner, write_cli_config: Callable[..., Path]
) -> None:
    write_cli_config(space_id=SPACE_ID)
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0
    assert "already exists" in result.stdout


def test_config_set_writes_key(runner: CliRunner, cli_config_path: Path) -> None:
    result = runner.invoke(cli.app, ["config", "set", "space_id", SPACE_ID])
    assert result.exit_code == 0
    assert "updated space_id" in result.stdout
    data = tomllib.loads(cli_config_path.read_text(encoding="utf-8"))
    assert data["space_id"] == SPACE_ID


def test_config_set_preserves_existing_keys(
    runner: CliRunner, cli_config_path: Path, write_cli_config: Callable[..., Path]
) -> None:
    write_cli_config(trigger_prefix="keep-me:")
    result = runner.invoke(cli.app, ["config", "set", "space_id", SPACE_ID])
    assert result.exit_code == 0
    data = tomllib.loads(cli_config_path.read_text(encoding="utf-8"))
    assert data["space_id"] == SPACE_ID
    assert data["trigger_prefix"] == "keep-me:"


def test_config_set_rejects_unknown_key(runner: CliRunner, cli_config_path: Path) -> None:
    # config_set routes through the shared merge_config_values validation, which
    # rejects an unknown key up front; the CLI surfaces it as a clean non-zero
    # exit with an actionable message (no leaked traceback) and writes nothing.
    result = runner.invoke(cli.app, ["config", "set", "bogus_key", "x"])
    assert result.exit_code == 2
    assert "bogus_key" in result.output
    assert not cli_config_path.exists()


def test_config_set_missing_value_arg_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["config", "set", "space_id"])
    assert result.exit_code != 0


def test_config_show_masks_secret(runner: CliRunner, write_cli_config: Callable[..., Path]) -> None:
    write_cli_config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "TEST_KEY" not in result.stdout
    assert "TEST_TOKEN" not in result.stdout
    assert SPACE_ID in result.stdout


def test_config_no_subcommand_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["config"])
    assert result.exit_code != 0
    assert "Usage" in result.stdout


# --------------------------------------------------------------------------- #
# chat send.
# --------------------------------------------------------------------------- #


def test_chat_send_calls_send_webhook(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(
        cli.app,
        ["chat", "send", "--text", "build green", "--status", "success"],
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "sent"
    sent.assert_called_once()
    _config_arg, msg = sent.call_args.args
    assert isinstance(msg, ChatMessage)
    assert msg.kind == "status"
    assert msg.status == "success"
    assert msg.text == "build green"


def test_chat_send_forwards_correlation_id(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(
        cli.app,
        ["chat", "send", "--text", "hi", "--correlation-id", "abc-123"],
    )
    assert result.exit_code == 0
    _config_arg, msg = sent.call_args.args
    assert msg.correlation_id == "abc-123"
    assert msg.status == "info"  # default


def test_chat_send_defaults_to_config_send_envelope(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flag the resolved config's send_envelope (default False) is used."""
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(cli.app, ["chat", "send", "--text", "hi"])
    assert result.exit_code == 0
    config_arg, _msg = sent.call_args.args
    assert config_arg.send_envelope is False


def test_chat_send_envelope_flag_overrides_config_true(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--envelope`` forces the envelope on even when config has it off."""
    write_cli_config(webhook_url=WEBHOOK_URL, send_envelope=False)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(cli.app, ["chat", "send", "--text", "hi", "--envelope"])
    assert result.exit_code == 0
    config_arg, _msg = sent.call_args.args
    assert config_arg.send_envelope is True


def test_chat_send_no_envelope_flag_overrides_config_false(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-envelope`` forces the envelope off even when config has it on."""
    write_cli_config(webhook_url=WEBHOOK_URL, send_envelope=True)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(cli.app, ["chat", "send", "--text", "hi", "--no-envelope"])
    assert result.exit_code == 0
    config_arg, _msg = sent.call_args.args
    assert config_arg.send_envelope is False


def test_chat_send_thread_key_forwarded_and_prints_thread_name(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--thread-key`` is threaded through send_webhook; the thread.name prints."""
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock(return_value=f"{SPACE_ID}/threads/T-new")
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(
        cli.app,
        ["chat", "send", "--text", "deploying", "--thread-key", "deploy-7"],
    )
    assert result.exit_code == 0
    # thread_key was forwarded to the transport.
    assert sent.call_args.kwargs["thread_key"] == "deploy-7"
    # The returned thread.name is surfaced on stderr (stdout stays the "sent" line).
    assert "sent" in result.stdout
    assert f"{SPACE_ID}/threads/T-new" in result.stderr


def test_chat_send_without_thread_key_passes_none_and_no_thread_line(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``--thread-key`` the transport gets ``None`` and nothing is printed."""
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock(return_value=None)
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)

    result = runner.invoke(cli.app, ["chat", "send", "--text", "hi"])
    assert result.exit_code == 0
    assert sent.call_args.kwargs["thread_key"] is None
    assert "thread:" not in result.stderr


def test_chat_send_missing_text_option_exits_nonzero(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(webhook_url=WEBHOOK_URL)
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)
    result = runner.invoke(cli.app, ["chat", "send", "--status", "info"])
    assert result.exit_code != 0
    sent.assert_not_called()


def test_chat_send_missing_webhook_config_fails_fast(
    runner: CliRunner,
    cli_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No webhook_url anywhere -> Config.load(require=...) raises ValueError.
    sent = MagicMock()
    monkeypatch.setattr("claude_google_chat.chat.send_webhook", sent)
    result = runner.invoke(cli.app, ["chat", "send", "--text", "hi"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "webhook_url" in str(result.exception)
    sent.assert_not_called()


# --------------------------------------------------------------------------- #
# auth login.
# --------------------------------------------------------------------------- #


def test_auth_login_invokes_flow(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(oauth_client_file="/tmp/client.json")
    login = MagicMock()
    monkeypatch.setattr("claude_google_chat.auth.login", login)

    result = runner.invoke(cli.app, ["auth", "login"])
    assert result.exit_code == 0
    assert "OAuth token cached" in result.stdout
    login.assert_called_once()


def test_auth_login_missing_client_file_fails_fast(
    runner: CliRunner,
    cli_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = MagicMock()
    monkeypatch.setattr("claude_google_chat.auth.login", login)
    result = runner.invoke(cli.app, ["auth", "login"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "oauth_client_file" in str(result.exception)
    login.assert_not_called()


# --------------------------------------------------------------------------- #
# listen.
# --------------------------------------------------------------------------- #


def test_listen_runs_and_returns_exit_code(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)

    result = runner.invoke(cli.app, ["listen", "--once"])
    assert result.exit_code == 0
    run.assert_called_once()
    assert run.call_args.kwargs["once"] is True


def test_listen_propagates_nonzero_exit_code(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    monkeypatch.setattr("claude_google_chat.listener.run", MagicMock(return_value=3))
    result = runner.invoke(cli.app, ["listen"])
    assert result.exit_code == 3


def test_listen_timeout_override_replaces_config(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)

    result = runner.invoke(cli.app, ["listen", "--timeout", "42"])
    assert result.exit_code == 0
    config_arg = run.call_args.args[0]
    assert config_arg.listen_timeout == 42.0


def test_listen_missing_config_fails_fast(
    runner: CliRunner,
    cli_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)
    result = runner.invoke(cli.app, ["listen"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    run.assert_not_called()


def test_listen_bad_timeout_value_exits_nonzero(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    monkeypatch.setattr("claude_google_chat.listener.run", MagicMock(return_value=0))
    result = runner.invoke(cli.app, ["listen", "--timeout", "not-a-number"])
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# status / setup.
# --------------------------------------------------------------------------- #


def test_status_reports_readiness_flags(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
) -> None:
    write_cli_config(
        webhook_url=WEBHOOK_URL,
        space_id=SPACE_ID,
        oauth_client_file="/tmp/client.json",
    )
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "send ready: True" in result.stdout
    assert "read ready: True" in result.stdout
    # Secrets stay masked in the JSON dump.
    assert "TEST_TOKEN" not in result.stdout


def test_status_reports_not_ready_when_empty(
    runner: CliRunner,
    cli_config_path: Path,
) -> None:
    # No config file present -> env-only empty config.
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "send ready: False" in result.stdout
    assert "read ready: False" in result.stdout


def test_setup_prints_config_path_and_keys(
    runner: CliRunner,
    cli_config_path: Path,
) -> None:
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert str(cli_config_path) in result.stdout
    assert "webhook_url" in result.stdout
    assert "space_id" in result.stdout


# --------------------------------------------------------------------------- #
# clear.
# --------------------------------------------------------------------------- #


def test_clear_deletes_only_trigger_messages(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    messages = [
        {"name": f"{SPACE_ID}/messages/1", "text": f"{DEFAULT_TRIGGER_PREFIX} deploy"},
        {"name": f"{SPACE_ID}/messages/2", "text": "ordinary chatter"},
        {"name": f"{SPACE_ID}/messages/3", "text": f"  {DEFAULT_TRIGGER_PREFIX} ship"},
    ]
    monkeypatch.setattr("claude_google_chat.chat.list_messages", lambda config: messages)
    deleted: list[str] = []
    monkeypatch.setattr(
        "claude_google_chat.chat.delete_message",
        lambda config, name: deleted.append(name),
    )

    result = runner.invoke(cli.app, ["clear"])
    assert result.exit_code == 0
    assert "deleted 2 message(s)" in result.stdout
    assert deleted == [f"{SPACE_ID}/messages/1", f"{SPACE_ID}/messages/3"]


def test_clear_missing_config_fails_fast(
    runner: CliRunner,
    cli_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("claude_google_chat.chat.list_messages", MagicMock())
    monkeypatch.setattr("claude_google_chat.chat.delete_message", MagicMock())
    result = runner.invoke(cli.app, ["clear"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)


def test_clear_trigger_prefix_override(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    messages = [
        {"name": f"{SPACE_ID}/messages/1", "text": "ops: deploy"},
        {"name": f"{SPACE_ID}/messages/2", "text": f"{DEFAULT_TRIGGER_PREFIX} ignored"},
    ]
    monkeypatch.setattr("claude_google_chat.chat.list_messages", lambda config: messages)
    deleted: list[str] = []
    monkeypatch.setattr(
        "claude_google_chat.chat.delete_message",
        lambda config, name: deleted.append(name),
    )
    result = runner.invoke(cli.app, ["clear", "--trigger-prefix", "ops:"])
    assert result.exit_code == 0
    assert deleted == [f"{SPACE_ID}/messages/1"]


# --------------------------------------------------------------------------- #
# config get.
# --------------------------------------------------------------------------- #


def test_config_get_prints_value(runner: CliRunner, write_cli_config: Callable[..., Path]) -> None:
    write_cli_config(space_id=SPACE_ID)
    result = runner.invoke(cli.app, ["config", "get", "space_id"])
    assert result.exit_code == 0
    assert result.stdout.strip() == SPACE_ID


def test_config_get_masks_secret(runner: CliRunner, write_cli_config: Callable[..., Path]) -> None:
    write_cli_config(webhook_url=WEBHOOK_URL)
    result = runner.invoke(cli.app, ["config", "get", "webhook_url"])
    assert result.exit_code == 0
    assert "TEST_KEY" not in result.stdout
    assert "TEST_TOKEN" not in result.stdout


def test_config_get_unknown_key_exits_code_2(runner: CliRunner, cli_config_path: Path) -> None:
    result = runner.invoke(cli.app, ["config", "get", "not_a_real_key"])
    assert result.exit_code == 2
    assert "unknown config key" in result.stderr


# --------------------------------------------------------------------------- #
# auth login --client-file override.
# --------------------------------------------------------------------------- #


def test_auth_login_client_file_override(
    runner: CliRunner,
    cli_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = tmp_path / "client_secret.json"
    client.write_text("{}", encoding="utf-8")
    login = MagicMock()
    monkeypatch.setattr("claude_google_chat.auth.login", login)
    result = runner.invoke(cli.app, ["auth", "login", "--client-file", str(client)])
    assert result.exit_code == 0
    login.assert_called_once()
    config_arg = login.call_args.args[0]
    assert config_arg.oauth_client_file == str(client)


def test_auth_login_client_file_missing_path_exits_nonzero(
    runner: CliRunner,
    cli_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = MagicMock()
    monkeypatch.setattr("claude_google_chat.auth.login", login)
    result = runner.invoke(cli.app, ["auth", "login", "--client-file", "/does/not/exist.json"])
    assert result.exit_code != 0
    login.assert_not_called()


# --------------------------------------------------------------------------- #
# listen --space-id override.
# --------------------------------------------------------------------------- #


def test_listen_space_id_override(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)
    result = runner.invoke(cli.app, ["listen", "--space-id", "spaces/OTHER"])
    assert result.exit_code == 0
    config_arg = run.call_args.args[0]
    assert config_arg.space_id == "spaces/OTHER"


def test_listen_thread_flags_override_config_threads(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated ``--thread`` flags become the config thread filter."""
    write_cli_config(space_id=SPACE_ID, oauth_client_file="/tmp/client.json")
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)
    result = runner.invoke(
        cli.app,
        [
            "listen",
            "--thread",
            "spaces/AAAA/threads/T1",
            "--thread",
            "spaces/AAAA/threads/T2",
        ],
    )
    assert result.exit_code == 0
    config_arg = run.call_args.args[0]
    assert config_arg.threads == ("spaces/AAAA/threads/T1", "spaces/AAAA/threads/T2")


def test_listen_without_thread_flag_preserves_config_threads(
    runner: CliRunner,
    write_cli_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--thread`` keeps the configured ``threads`` value."""
    write_cli_config(
        space_id=SPACE_ID,
        oauth_client_file="/tmp/client.json",
        threads=["spaces/AAAA/threads/CFG"],
    )
    run = MagicMock(return_value=0)
    monkeypatch.setattr("claude_google_chat.listener.run", run)
    result = runner.invoke(cli.app, ["listen"])
    assert result.exit_code == 0
    config_arg = run.call_args.args[0]
    assert config_arg.threads == ("spaces/AAAA/threads/CFG",)


# --------------------------------------------------------------------------- #
# completion command.
# --------------------------------------------------------------------------- #


def test_completion_bash_prints_script(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["completion", "bash"])
    assert result.exit_code == 0
    assert "_CGC_COMPLETE" in result.stdout
    assert "complete" in result.stdout


def test_completion_unsupported_shell_exits_code_2(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["completion", "powershell"])
    assert result.exit_code == 2
    assert "powershell" in result.stderr


def test_completion_install_appends_to_rc(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(cli.app, ["completion", "zsh", "--install"])
    assert result.exit_code == 0
    assert "completion installed" in result.stdout
    rc = tmp_path / ".zshrc"
    assert rc.exists()
    assert "_CGC_COMPLETE=source_zsh" in rc.read_text(encoding="utf-8")


def test_completion_undetectable_shell_exits_code_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("claude_google_chat.cli.detect_shell", lambda: None)
    result = runner.invoke(cli.app, ["completion"])
    assert result.exit_code == 2
    assert "could not detect" in result.stderr
