"""End-to-end user journeys through the public CLI / API.

Each test reads as a human-like scenario: a person configures the tool, a
teammate posts a message, the operator runs ``cgc``. We drive the real Typer CLI
(via ``CliRunner``) and the public ``serve``/``bootstrap`` entry points, with only
the genuine external boundaries faked:

- incoming-webhook HTTP (the ``responses`` fixture ``mocked_webhook``);
- the Google Chat REST API (``FakeChatService``, injected by monkeypatching the
  ``build_app_service`` discovery boundary so no network/discovery call is made);
- Google service-account credential loading (monkeypatched, the heavy auth
  boundary), since the CLI ``serve``/``bootstrap`` commands require it before the
  faked service is reached.

Config is supplied the supported way: environment variables (``CGC_*``) read by
``Config.load``, so no real OS config directory is touched.
"""

from __future__ import annotations

import json

import pytest
import responses as responses_lib
from typer.testing import CliRunner

import claude_google_chat.chat as chat_module
from claude_google_chat.bootstrap import ChatAppNotConfiguredError
from claude_google_chat.bootstrap import bootstrap as run_bootstrap
from claude_google_chat.cli import app
from claude_google_chat.config import Config
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, STATUS_EMOJI
from claude_google_chat.serve import run as run_serve

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


def _http_error(status: int, message: str) -> Exception:
    """Build a googleapiclient ``HttpError`` as the real Chat API would raise."""
    from googleapiclient.errors import HttpError

    resp = type("Resp", (), {"status": status, "reason": "Error"})()
    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(resp, content, uri="https://chat.googleapis.com/v1/...")


# --------------------------------------------------------------------------- #
# Journey 1: First-time setup -> configure webhook -> send a status ping.
# --------------------------------------------------------------------------- #


def test_journey_first_time_setup_and_status_ping(
    mocked_webhook,
    webhook_payloads,
    frozen_clock,
) -> None:
    """A new user configures only a webhook and sends a status ping.

    They run ``cgc chat send`` with the webhook URL supplied via the environment;
    the webhook must receive the correctly-formatted structured payload.
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
    assert text.splitlines()[0] == f"{STATUS_EMOJI['success']} deploy finished"
    envelope = json.loads(text.split("```")[1])
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
# Journey 2: Inbound command -> serve loop picks it up -> structured reply posted.
# --------------------------------------------------------------------------- #


def test_journey_inbound_command_gets_structured_reply(
    monkeypatch,
    fake_chat_service,
    human_trigger_message,
) -> None:
    """A teammate posts 'claude-command: status'; ``cgc serve --once`` replies.

    The Chat API is faked and injected at the discovery boundary; the serve loop
    fetches the message, recognises the owner trigger, and posts a structured
    'result' reply back to the space.
    """
    # The owner posts a recognised trigger command.
    inbound = dict(human_trigger_message)
    inbound["text"] = f"{DEFAULT_TRIGGER_PREFIX} status"
    inbound["sender"] = {"type": "HUMAN", "email": "owner@example.com"}
    fake_chat_service.list_pages = [{"messages": [inbound]}]

    # Fake the heavy boundaries: service-account creds + discovery build.
    monkeypatch.setattr(chat_module, "load_app_credentials", lambda config: object())
    monkeypatch.setattr(chat_module, "build_app_service", lambda config: fake_chat_service)

    config = Config(
        service_account_file="/tmp/sa.json",
        space_id="spaces/AAAA",
        owner_email="owner@example.com",
        trigger_prefix=DEFAULT_TRIGGER_PREFIX,
    )

    exit_code = run_serve(config, once=True)

    assert exit_code == 0
    assert len(fake_chat_service.create_calls) == 1
    posted_text = fake_chat_service.create_calls[0]["body"]["text"]
    envelope = json.loads(posted_text.split("```")[1])
    assert envelope["kind"] == "result"
    assert envelope["status"] == "success"
    assert "status" in envelope["text"]


# --------------------------------------------------------------------------- #
# Journey 3: Plain (no-prefix) message -> used as context, NOT executed.
# --------------------------------------------------------------------------- #


def test_journey_plain_message_is_not_executed(
    monkeypatch,
    fake_chat_service,
    human_plain_message,
) -> None:
    """A plain chat line (no trigger prefix) produces no command side effects."""
    fake_chat_service.list_pages = [{"messages": [human_plain_message]}]

    monkeypatch.setattr(chat_module, "load_app_credentials", lambda config: object())
    monkeypatch.setattr(chat_module, "build_app_service", lambda config: fake_chat_service)

    config = Config(
        service_account_file="/tmp/sa.json",
        space_id="spaces/AAAA",
        owner_email="owner@example.com",
        trigger_prefix=DEFAULT_TRIGGER_PREFIX,
    )

    exit_code = run_serve(config, once=True)

    assert exit_code == 0
    # No reply was posted: the plain message is context only, never a command.
    assert fake_chat_service.create_calls == []


# --------------------------------------------------------------------------- #
# Journey 4: Bootstrap before the Chat app is configured -> exact gate message.
# --------------------------------------------------------------------------- #


def test_journey_bootstrap_before_app_configured_hits_gate(
    monkeypatch,
    fake_chat_service,
) -> None:
    """Bootstrapping before the manual Chat-app Configuration step is done.

    The Chat API rejects the membership call with PERMISSION_DENIED; the journey
    ends with the exact actionable gate instructions, not a raw stack trace.
    """
    fake_chat_service.member_create_error = _http_error(403, "PERMISSION_DENIED")

    monkeypatch.setattr(chat_module, "load_app_credentials", lambda config, scopes=None: object())
    monkeypatch.setattr(chat_module, "build_app_service", lambda config: fake_chat_service)

    config = Config(
        service_account_file="/tmp/sa.json",
        space_id="spaces/AAAA",
        project_id="test-project",
        pubsub_topic="chat-events",
    )

    with pytest.raises(ChatAppNotConfiguredError) as exc_info:
        run_bootstrap(config)

    message = str(exc_info.value)
    assert "The Google Chat app is not configured yet" in message
    assert "cgc bootstrap" in message
    assert "App status: LIVE" in message
    assert "test-project" in message  # console URL carries the project id
    # It never reached the events-subscription / config-write steps.
    assert fake_chat_service.space_create_calls == []


def test_journey_bootstrap_gate_via_cli_exits_two(
    monkeypatch,
    fake_chat_service,
    tmp_path,
) -> None:
    """The same gate surfaced through ``cgc bootstrap`` exits 2 with instructions."""
    fake_chat_service.member_create_error = _http_error(404, "NOT_FOUND")
    monkeypatch.setattr(chat_module, "load_app_credentials", lambda config, scopes=None: object())
    monkeypatch.setattr(chat_module, "build_app_service", lambda config: fake_chat_service)

    # A real (empty) SA file so Config.require_keys passes; auth itself is faked.
    sa_file = tmp_path / "sa.json"
    sa_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        ["bootstrap"],
        env=_cli_env(
            CGC_SERVICE_ACCOUNT_FILE=str(sa_file),
            CGC_SPACE_ID="spaces/AAAA",
            CGC_PROJECT_ID="test-project",
            CGC_PUBSUB_TOPIC="chat-events",
        ),
    )

    assert result.exit_code == 2
    assert "not configured yet" in result.output


# --------------------------------------------------------------------------- #
# Journey 5: Multiple inbound messages -> processed once each (dedup), since kept.
# --------------------------------------------------------------------------- #


def test_journey_multiple_messages_dedup_and_lastseen(
    monkeypatch,
    fake_chat_service,
    make_raw_message,
) -> None:
    """Several inbound triggers are each handled exactly once across polls.

    Two distinct owner commands arrive on the first poll; the newest createTime
    becomes the ``since`` filter so a re-poll of the same page does not re-handle
    them (dedup by message name) and the last-seen timestamp is persisted.
    """
    first = make_raw_message(
        name="spaces/AAAA/messages/m1",
        text=f"{DEFAULT_TRIGGER_PREFIX} build",
        email="owner@example.com",
        create_time="2026-06-20T00:00:01Z",
        thread=None,
    )
    second = make_raw_message(
        name="spaces/AAAA/messages/m2",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy",
        email="owner@example.com",
        create_time="2026-06-20T00:00:05Z",
        thread=None,
    )
    fake_chat_service.list_pages = [{"messages": [first, second]}]

    monkeypatch.setattr(chat_module, "load_app_credentials", lambda config: object())
    monkeypatch.setattr(chat_module, "build_app_service", lambda config: fake_chat_service)

    config = Config(
        service_account_file="/tmp/sa.json",
        space_id="spaces/AAAA",
        owner_email="owner@example.com",
        trigger_prefix=DEFAULT_TRIGGER_PREFIX,
    )

    from claude_google_chat.serve import Responder

    responder = Responder(config)

    first_batch = responder.run(once=True)
    assert len(first_batch) == 2
    assert len(fake_chat_service.create_calls) == 2

    # The last-seen (since) is the newest createTime.
    assert responder._since == "2026-06-20T00:00:05Z"

    # Re-poll the identical page: dedup by message name => nothing re-handled.
    fake_chat_service._page_cursor = 0
    second_batch = responder.run(once=True)
    assert second_batch == []
    assert len(fake_chat_service.create_calls) == 2

    # The re-poll sent the since filter so the API would only return newer ones.
    last_list = fake_chat_service.list_calls[-1]
    assert last_list["filter"] == 'createTime > "2026-06-20T00:00:05Z"'


# --------------------------------------------------------------------------- #
# Journey 6: Webhook failure (HTTP 500) -> graceful error, no crash.
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
