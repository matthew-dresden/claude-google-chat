"""Typer CLI for claude-google-chat (``cgc`` console script).

Subcommands:
    config init|show|get|set  manage the user config file
    auth login                complete the OAuth installed-app flow
    chat send                 send a status ping via the webhook
    listen                    run the inbound listener
    clear                     delete trigger messages from the space
    status                    show resolved configuration health
    completion                print/install the shell-completion script

All side-effecting failures fail fast with a non-zero exit code and a clear,
non-secret message.

Shell completion is available two ways: Typer's native ``--install-completion``
/ ``--show-completion`` flags, and the friendlier ``cgc completion <shell>``
command. Dynamic value completers (config keys, ``--status`` values, ``--shell``
choices, file paths, and config-derived values) are wired from
:mod:`claude_google_chat.completion`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from claude_google_chat import __version__
from claude_google_chat.completion import (
    SUPPORTED_COMPLETION_SHELLS,
    complete_config_key,
    complete_shell,
    complete_space_id,
    complete_status,
    complete_trigger_prefix,
    detect_shell,
    install_completion_line,
    render_completion_script,
)
from claude_google_chat.config import (
    Config,
    default_config_path,
    merge_and_write_config,
    write_config,
)
from claude_google_chat.messages import ChatMessage

APP_EPILOG = (
    "Enable tab completion with 'cgc completion bash --install' (or zsh/fish), "
    "or Typer's native 'cgc --install-completion'. Configuration lives in the OS "
    "config directory; run 'cgc setup' to see the path and required keys."
)

app = typer.Typer(
    name="cgc",
    help="Two-way Google Chat ChatOps integration for Claude Code.",
    epilog=APP_EPILOG,
    no_args_is_help=True,
    add_completion=True,
)

config_app = typer.Typer(
    help="Manage the user configuration file (init/show/get/set).",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Manage Google OAuth credentials.", no_args_is_help=True)
chat_app = typer.Typer(help="Send messages to Google Chat.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")
app.add_typer(chat_app, name="chat")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _apply_overrides(
    config: Config,
    *,
    space_id: str | None = None,
    timeout: float | None = None,
    trigger_prefix: str | None = None,
) -> Config:
    """Return ``config`` with any non-``None`` CLI overrides applied.

    Centralises the "replace the field only when the flag was given" pattern
    shared by ``listen``/``clear`` so the override logic lives in one place (DRY)
    instead of being repeated per command.
    """
    if space_id is not None:
        config = replace(config, space_id=space_id)
    if timeout is not None:
        config = replace(config, listen_timeout=timeout)
    if trigger_prefix is not None:
        config = replace(config, trigger_prefix=trigger_prefix)
    return config


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Two-way Google Chat ChatOps integration for Claude Code.

    Run a command group with ``--help`` (e.g. ``cgc config --help``) to see its
    subcommands. Outbound status pings use an incoming webhook; inbound listening
    uses user OAuth credentials. All required configuration is resolved from
    environment variables or the user config file, failing fast with a clear
    message when a value is missing.
    """


@config_app.command("init")
def config_init() -> None:
    """Create an empty config file under the OS config directory if absent."""
    path = default_config_path()
    if path.exists():
        typer.echo(f"config already exists at {path}")
        return
    write_config({}, path=path)
    typer.echo(f"created config at {path}")


@config_app.command("show")
def config_show() -> None:
    """Show the resolved configuration with secrets masked."""
    config = Config.load()
    typer.echo(json.dumps(config.redacted(), indent=2, sort_keys=True))


@config_app.command("get")
def config_get(
    key: str = typer.Argument(
        ...,
        help="Config key to read (secrets are masked).",
        autocompletion=complete_config_key,
    ),
) -> None:
    """Print one resolved config value, masking secrets.

    Reads the same merged (file + environment) view as ``config show`` and
    fails fast with a non-zero exit code if the key is unknown.
    """
    config = Config.load()
    redacted = config.redacted()
    if key not in redacted:
        valid = ", ".join(sorted(redacted))
        typer.echo(f"unknown config key {key!r}; valid keys: {valid}", err=True)
        raise typer.Exit(code=2)
    value = redacted[key]
    typer.echo("" if value is None else str(value))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ...,
        help="Config key to set.",
        autocompletion=complete_config_key,
    ),
    value: str = typer.Argument(..., help="Value to store."),
) -> None:
    """Set a single config key, preserving existing keys.

    Routes through the shared ``merge_config_values`` validation (via
    ``merge_and_write_config``) so an unknown key fails fast with a single
    consistent rule, before anything is written.
    """
    try:
        written = merge_and_write_config({key: value})
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"updated {key} in {written}")


@auth_app.command("login")
def auth_login(
    client_file: Annotated[
        Path | None,
        typer.Option(
            "--client-file",
            help="OAuth client-secrets JSON file (overrides config oauth_client_file).",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
) -> None:
    """Run the OAuth installed-app flow and cache the token.

    Reads the OAuth client-secrets file from ``--client-file`` if given,
    otherwise from the resolved ``oauth_client_file`` config value. Fails fast
    when neither is available.
    """
    from claude_google_chat.auth import login

    if client_file is not None:
        # Typer validated the path exists/is readable; override the config value.
        config = replace(Config.load(), oauth_client_file=str(client_file))
    else:
        config = Config.load(require=("oauth_client_file",))
    login(config)
    typer.echo("OAuth token cached")


@chat_app.command("send")
def chat_send(
    text: str = typer.Option(..., "--text", help="Message body (summary line)."),
    status: str = typer.Option(
        "info",
        "--status",
        help="Status label: info, working, success, error, or blocked.",
        autocompletion=complete_status,
    ),
    correlation_id: str | None = typer.Option(
        None,
        "--correlation-id",
        help="Optional id linking a result back to a command.",
    ),
    envelope: bool | None = typer.Option(
        None,
        "--envelope/--no-envelope",
        help=(
            "Append the machine-readable JSON envelope to the Chat text "
            "(--envelope) or send only the clean summary line (--no-envelope). "
            "Defaults to the resolved 'send_envelope' config value."
        ),
    ),
) -> None:
    """Send a structured status ping via the incoming webhook."""
    from claude_google_chat.chat import send_webhook

    config = Config.load(require=("webhook_url",))
    if envelope is not None:
        config = replace(config, send_envelope=envelope)
    msg = ChatMessage(
        kind="status",
        status=status,
        text=text,
        correlation_id=correlation_id,
    )
    send_webhook(config, msg)
    typer.echo("sent")


@app.command("setup")
def setup() -> None:
    """Print the configuration file location and required keys."""
    path = default_config_path()
    typer.echo(f"config file: {path}")
    typer.echo("required for send:  webhook_url (CGC_WEBHOOK_URL)")
    typer.echo("required for read:  space_id (CGC_SPACE_ID), oauth_client_file")
    typer.echo("use 'cgc config set <key> <value>' to populate")


@app.command("listen")
def listen(
    once: bool = typer.Option(False, "--once", help="Drain pending messages and exit."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Idle timeout in seconds (overrides config listen_timeout)."
    ),
    space_id: str | None = typer.Option(
        None,
        "--space-id",
        help="Chat space to read, e.g. spaces/AAAA (overrides config space_id).",
        autocompletion=complete_space_id,
    ),
) -> None:
    """Run the inbound listener, emitting one JSON line per new message."""
    from claude_google_chat.listener import run

    config = _apply_overrides(Config.load(), space_id=space_id, timeout=timeout)
    config.require_keys(("space_id", "oauth_client_file"))
    raise typer.Exit(code=run(config, once=once))


@app.command("clear")
def clear(
    trigger_prefix: str | None = typer.Option(
        None,
        "--trigger-prefix",
        help="Only delete messages starting with this prefix (overrides config).",
        autocompletion=complete_trigger_prefix,
    ),
) -> None:
    """Delete trigger-prefixed messages from the configured space."""
    from claude_google_chat.chat import delete_message, list_messages

    config = Config.load(require=("space_id", "oauth_client_file"))
    config = _apply_overrides(config, trigger_prefix=trigger_prefix)
    prefix = config.trigger_prefix
    deleted = 0
    for raw in list_messages(config):
        text = raw.get("text", "")
        name = raw.get("name", "")
        if name and text.strip().startswith(prefix):
            delete_message(config, name)
            deleted += 1
    typer.echo(f"deleted {deleted} message(s)")


@app.command("status")
def status() -> None:
    """Report which configuration values are present (secrets masked)."""
    config = Config.load()
    redacted = config.redacted()
    has_send = bool(config.webhook_url)
    has_read = bool(config.space_id and config.oauth_client_file)
    typer.echo(json.dumps(redacted, indent=2, sort_keys=True))
    typer.echo(f"send ready: {has_send}")
    typer.echo(f"read ready: {has_read}")


@app.command("completion")
def completion(
    shell: str | None = typer.Argument(
        None,
        help="Target shell: bash, zsh, or fish. Defaults to the detected shell.",
        autocompletion=complete_shell,
    ),
    install: bool = typer.Option(
        False,
        "--install",
        help="Append the completion line to the shell's rc file instead of printing.",
    ),
) -> None:
    """Print (or install) the tab-completion script for a shell.

    Without ``--install`` the completion source is printed to stdout so you can
    pipe it or add it yourself, e.g.::

        cgc completion bash >> ~/.bashrc

    With ``--install`` an idempotent line is appended to the shell's rc file
    (``~/.bashrc``, ``~/.zshrc``, or ``~/.config/fish/config.fish``) that
    evaluates the live completion source on shell start-up. Fails fast with a
    clear message for an unsupported or undetectable shell.
    """
    resolved = shell or detect_shell()
    if resolved is None:
        supported = ", ".join(SUPPORTED_COMPLETION_SHELLS)
        typer.echo(
            f"could not detect the current shell; pass one explicitly (supported: {supported})",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        if install:
            rc_path = install_completion_line(app.info.name or "cgc", resolved)
        else:
            script = render_completion_script(app.info.name or "cgc", resolved)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if install:
        typer.echo(f"{resolved} completion installed in {rc_path}")
        typer.echo("Completion will take effect in new shells (or 'source' the rc file).")
    else:
        typer.echo(script)


if __name__ == "__main__":
    app()
