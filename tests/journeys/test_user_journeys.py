"""End-to-end user journeys through the public CLI / API.

Each test reads as a human-like scenario: a person configures the tool, a
teammate posts a message, the operator runs ``cgc``. We drive the real Typer CLI
(via ``CliRunner``) and the public ``listen`` entry point, with only the genuine
external boundaries faked:

- incoming-webhook HTTP (the ``responses`` fixture ``mocked_webhook``);
- the Google Chat REST API (``FakeChatService``, injected by monkeypatching the
  ``chat.list_messages`` transport so no network/discovery call is made).

Config is supplied the supported way: environment variables (``CGC_*``) read by
``Config.load``, so no real OS config directory is touched.
"""

from __future__ import annotations

import json

import pytest
import responses as responses_lib
from typer.testing import CliRunner

from claude_google_chat.cli import app
from claude_google_chat.config import Config
from claude_google_chat.listener import run as run_listen
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, STATUS_EMOJI

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_default_config(monkeypatch, tmp_path) -> None:
    """Point the CLI's default config path at an empty tmp location.

    The CLI resolves config via ``Config.load()`` which reads
    ``config.default_config_path()``. A real config file exists under the user's
    OS config dir on this machine; redirecting it to a non-existent tmp path keeps
    every journey hermetic and env-driven (no real OS config is read).
    """
    import claude_google_chat.config as config_module

    missing = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "default_config_path", lambda: missing)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _cli_env(**overrides: str) -> dict[str, str]:
    """Build a CGC_* environment mapping for a CLI invocation.

    Only keys the journey needs are set; everything else stays unset so the CLI
    falls back to its real defaults (no real config file is read in tests because
    none exists under the isolated, non-existent default path).
    """
    return dict(overrides)


# --------------------------------------------------------------------------- #
# Journey 1: First-time setup -> configure webhook -> send a status ping.
# --------------------------------------------------------------------------- #


def test_journey_first_time_setup_and_status_ping(
    mocked_webhook,
    webhook_payloads,
    frozen_clock,
) -> None:
    """A new user configures only a webhook and sends a status ping.

    They run ``cgc chat send`` with the webhook URL supplied via the environment.
    With no ``--envelope`` flag and no ``send_envelope`` config the human-facing
    Chat payload is the clean, emoji-prefixed summary line alone (no fenced JSON);
    the machine-readable envelope is opt-in and covered separately. Passing
    ``--envelope`` then proves the JSON envelope is appended on demand.
    """
    webhook_url = (
        "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=TEST_KEY&token=TEST_TOKEN"
    )
    result = runner.invoke(
        app,
        ["chat", "send", "--text", "deploy finished", "--status", "success"],
        env=_cli_env(CGC_WEBHOOK_URL=webhook_url),
    )

    assert result.exit_code == 0, result.output
    assert "sent" in result.output

    payloads = webhook_payloads()
    assert len(payloads) == 1
    text = payloads[0]["text"]
    # Clean by default: just the summary line, no fenced JSON envelope.
    assert text == f"{STATUS_EMOJI['success']} deploy finished"
    assert "```" not in text

    # Opt-in: --envelope appends the machine-readable JSON envelope.
    enveloped = runner.invoke(
        app,
        ["chat", "send", "--text", "deploy finished", "--status", "success", "--envelope"],
        env=_cli_env(CGC_WEBHOOK_URL=webhook_url),
    )
    assert enveloped.exit_code == 0, enveloped.output
    payloads = webhook_payloads()
    assert len(payloads) == 2
    enveloped_text = payloads[1]["text"]
    assert enveloped_text.splitlines()[0] == f"{STATUS_EMOJI['success']} deploy finished"
    envelope = json.loads(enveloped_text.split("```")[1])
    assert envelope["kind"] == "status"
    assert envelope["status"] == "success"
    assert envelope["text"] == "deploy finished"
    assert envelope["ts"] == frozen_clock


def test_journey_status_ping_without_webhook_fails_fast() -> None:
    """Sending before configuring a webhook fails fast with an actionable error."""
    result = runner.invoke(
        app,
        ["chat", "send", "--text", "ping"],
        env=_cli_env(),
    )
    assert result.exit_code != 0
    assert "webhook_url" in str(result.output) + str(result.exception)


# --------------------------------------------------------------------------- #
# Journey 2: Inbound command -> listener emits it once; a plain line is skipped.
# --------------------------------------------------------------------------- #


def test_journey_listen_emits_trigger_and_skips_plain(
    monkeypatch,
    human_trigger_message,
    human_plain_message,
    frozen_clock,
    capsys,
    tmp_path,
) -> None:
    """A teammate posts 'claude: deploy prod'; ``cgc listen --once`` emits it.

    The Chat REST transport is faked at ``chat.list_messages`` and returns a
    trigger-prefixed message alongside a plain (non-trigger) line. The listener
    surfaces only the structured command as one JSON line and exits 0; the plain
    line is context only and never emitted.
    """
    monkeypatch.setattr(
        "claude_google_chat.listener.list_messages",
        lambda config, since=None: [human_trigger_message, human_plain_message],
    )

    config = Config(
        space_id="spaces/AAAA",
        trigger_prefix=DEFAULT_TRIGGER_PREFIX,
        state_file=str(tmp_path / "state.json"),
    )

    exit_code = run_listen(config, once=True)

    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    record = json.loads(out_lines[0])
    assert record["kind"] == "command"
    assert record["command"] == "deploy"
    assert record["args"] == ["prod"]


# --------------------------------------------------------------------------- #
# Journey 3: Multiple inbound messages -> processed once each (dedup), since kept.
# --------------------------------------------------------------------------- #


def test_journey_multiple_messages_dedup_and_lastseen(
    monkeypatch,
    fake_chat_service,
    make_raw_message,
    frozen_clock,
) -> None:
    """Several inbound triggers are each handled exactly once across polls.

    Two distinct commands arrive on the first poll; the newest createTime becomes
    the ``since`` filter so a re-poll of the same page does not re-emit them
    (dedup by message name). The real ``chat.list_messages`` request building /
    pagination runs against the injected fake service.
    """
    first = make_raw_message(
        name="spaces/AAAA/messages/m1",
        text=f"{DEFAULT_TRIGGER_PREFIX} build",
        create_time="2026-06-20T00:00:01Z",
        thread=None,
    )
    second = make_raw_message(
        name="spaces/AAAA/messages/m2",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy",
        create_time="2026-06-20T00:00:05Z",
        thread=None,
    )
    fake_chat_service.list_pages = [{"messages": [first, second]}]

    import claude_google_chat.chat as chat_module

    monkeypatch.setattr(chat_module, "_build_service", lambda config: fake_chat_service)

    from claude_google_chat.listener import Listener

    config = Config(
        space_id="spaces/AAAA",
        trigger_prefix=DEFAULT_TRIGGER_PREFIX,
    )
    listener = Listener(config)

    first_batch = list(listener.iter_new_messages(once=True))
    assert [m.command for m in first_batch] == ["build", "deploy"]

    # Re-poll the identical page: dedup by message name => nothing re-emitted.
    fake_chat_service._page_cursor = 0
    second_batch = list(listener.iter_new_messages(once=True))
    assert second_batch == []

    # The re-poll sent the since filter so the API would only return newer ones.
    last_list = fake_chat_service.list_calls[-1]
    assert last_list["filter"] == 'createTime > "2026-06-20T00:00:05Z"'


# --------------------------------------------------------------------------- #
# Journey 4: Webhook failure (HTTP 500) -> graceful error, no crash.
# --------------------------------------------------------------------------- #


def test_journey_webhook_http_500_fails_gracefully(mocked_webhook) -> None:
    """A 500 from the webhook surfaces a clean non-zero CLI exit, no traceback leak."""
    webhook_url = (
        "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=TEST_KEY&token=TEST_TOKEN"
    )
    mocked_webhook.reset()
    mocked_webhook.add(responses_lib.POST, webhook_url, json={}, status=500)

    result = runner.invoke(
        app,
        ["chat", "send", "--text", "ship it", "--status", "info"],
        env=_cli_env(CGC_WEBHOOK_URL=webhook_url),
    )

    # The CLI did not crash the process abnormally; it returns a non-zero code
    # and the raised HTTPError carries the status without leaking the secret.
    assert result.exit_code != 0
    rendered = str(result.output) + str(result.exception)
    assert "500" in rendered
    assert "TEST_TOKEN" not in rendered


def test_journey_webhook_failure_does_not_swallow_error(mocked_webhook) -> None:
    """The failure is surfaced (fail-fast), not silently swallowed as success."""
    webhook_url = (
        "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=TEST_KEY&token=TEST_TOKEN"
    )
    mocked_webhook.reset()
    mocked_webhook.add(responses_lib.POST, webhook_url, json={}, status=503)

    result = runner.invoke(
        app,
        ["chat", "send", "--text", "still failing"],
        env=_cli_env(CGC_WEBHOOK_URL=webhook_url),
    )
    assert result.exit_code != 0
    # The success confirmation must NOT be printed on failure.
    assert "sent" not in result.output
