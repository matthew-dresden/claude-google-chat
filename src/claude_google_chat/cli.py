"""Typer CLI for claude-google-chat (``cgc`` console script).

Subcommands:
    config init|show|set   manage the user config file
    auth login             complete the OAuth installed-app flow
    chat send              send a status ping via the webhook
    bootstrap              service-account setup Terraform can't do (join/create
                           space, create Workspace Events subscription, merge config)
    serve                  always-listening responder (app auth)
    listen                 run the inbound listener
    clear                  delete trigger messages from the space
    status                 show resolved configuration health

All side-effecting failures fail fast with a non-zero exit code and a clear,
non-secret message.
"""

from __future__ import annotations

import json

import typer

from claude_google_chat import __version__
from claude_google_chat.config import (
    Config,
    default_config_path,
    write_config,
)
from claude_google_chat.messages import ChatMessage

app = typer.Typer(
    name="cgc",
    help="Two-way Google Chat ChatOps integration for Claude Code.",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(help="Manage the user configuration file.", no_args_is_help=True)
auth_app = typer.Typer(help="Manage Google OAuth credentials.", no_args_is_help=True)
chat_app = typer.Typer(help="Send messages to Google Chat.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")
app.add_typer(chat_app, name="chat")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


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
    """Top-level entry point; handles the global ``--version`` flag."""


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


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key to set."),
    value: str = typer.Argument(..., help="Value to store."),
) -> None:
    """Set a single config key, preserving existing keys."""
    path = default_config_path()
    existing: dict[str, object] = {}
    if path.exists():
        import tomllib

        existing = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    existing[key] = value
    written = write_config(existing, path=path)
    typer.echo(f"updated {key} in {written}")


@auth_app.command("login")
def auth_login() -> None:
    """Run the OAuth installed-app flow and cache the token."""
    from claude_google_chat.auth import login

    config = Config.load(require=("oauth_client_file",))
    login(config)
    typer.echo("OAuth token cached")


@chat_app.command("send")
def chat_send(
    text: str = typer.Option(..., "--text", help="Message body."),
    status: str = typer.Option("info", "--status", help="Status label."),
    correlation_id: str | None = typer.Option(
        None, "--correlation-id", help="Optional correlation id."
    ),
) -> None:
    """Send a structured status ping via the incoming webhook."""
    from claude_google_chat.chat import send_webhook

    config = Config.load(require=("webhook_url",))
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


@app.command("bootstrap")
def bootstrap() -> None:
    """Run service-account setup that Terraform cannot do.

    Joins or creates the target Chat space, registers the Google Workspace
    Events ``message.created`` subscription to the Pub/Sub topic, and merges the
    discovered values into ``config.toml``. Fails fast with exact instructions
    if the manual Chat app Configuration console step is not yet done.
    """
    from claude_google_chat.bootstrap import ChatAppNotConfiguredError
    from claude_google_chat.bootstrap import bootstrap as run_bootstrap

    config = Config.load(require=("service_account_file", "pubsub_topic"))
    try:
        result = run_bootstrap(config)
    except ChatAppNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if result.created_space:
        typer.echo(f"created space {result.space_id}")
    elif result.joined_space:
        typer.echo(f"joined space {result.space_id}")
    else:
        typer.echo(f"already a member of space {result.space_id}")
    typer.echo(f"subscription: {result.subscription_name}")
    typer.echo(f"events topic: {result.pubsub_topic}")
    typer.echo(f"config written: {result.config_path}")


@app.command("serve")
def serve(
    once: bool = typer.Option(False, "--once", help="Handle pending owner messages once and exit."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Idle timeout in seconds (overrides config)."
    ),
) -> None:
    """Run the always-listening responder, replying to owner messages as the app.

    Polls the configured space using service-account (app) credentials and, for
    each new owner message starting with the trigger prefix, posts a structured
    reply via the Chat API. Exits non-zero on idle timeout (when configured).
    """
    from dataclasses import replace

    from claude_google_chat.serve import run as run_serve

    config = Config.load(require=("service_account_file", "space_id"))
    if timeout is not None:
        config = replace(config, listen_timeout=timeout)
    code = run_serve(config, once=once)
    raise typer.Exit(code=code)


@app.command("listen")
def listen(
    once: bool = typer.Option(False, "--once", help="Drain pending messages and exit."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Idle timeout in seconds (overrides config)."
    ),
) -> None:
    """Run the inbound listener, emitting one JSON line per new message."""
    from dataclasses import replace

    from claude_google_chat.listener import run

    config = Config.load(require=("space_id", "oauth_client_file"))
    if timeout is not None:
        config = replace(config, listen_timeout=timeout)
    code = run(config, once=once)
    raise typer.Exit(code=code)


@app.command("clear")
def clear() -> None:
    """Delete trigger-prefixed messages from the configured space."""
    from claude_google_chat.chat import delete_message, list_messages

    config = Config.load(require=("space_id", "oauth_client_file"))
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


if __name__ == "__main__":
    app()
