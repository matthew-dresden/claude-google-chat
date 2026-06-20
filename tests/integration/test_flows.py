"""Integration tests: real components wired together, only external boundaries mocked.

Unlike the unit tests (which inject fake ``fetcher`` callables directly), these
exercise the *real* transport functions in :mod:`claude_google_chat.chat`
(``send_webhook``, ``list_messages``) and the real
:class:`~claude_google_chat.listener.Listener` loop wired to them. The only
things stubbed are the two genuine external boundaries:

- the **incoming webhook HTTP POST** (the ``responses`` library, via ``mocked_webhook``),
  so the real ``requests.post`` path in ``send_webhook`` runs; and
- the **Google Chat REST API** (the ``FakeChatService`` fixture), injected by
  monkeypatching ``chat._build_service`` so ``list_messages`` runs its real
  request-building / pagination code without a ``googleapiclient.discovery.build``
  call.

Everything else (config, message parsing, formatting, HUMAN/BOT filtering, dedup,
high-water tracking) is the production code.
"""

from __future__ import annotations

import json

import pytest
import requests

import claude_google_chat.chat as chat_module
from claude_google_chat.chat import send_webhook
from claude_google_chat.listener import Listener
from claude_google_chat.messages import (
    DEFAULT_TRIGGER_PREFIX,
    STATUS_EMOJI,
    ChatMessage,
)

# --------------------------------------------------------------------------- #
# Webhook send path (real requests.post, mocked HTTP boundary).
# --------------------------------------------------------------------------- #


def test_send_webhook_posts_formatted_envelope(
    make_config,
    mocked_webhook,
    webhook_payloads,
    frozen_clock,
) -> None:
    """send_webhook builds the real format and POSTs it to the configured URL.

    Uses ``send_envelope=True`` so the real wire form includes the JSON envelope
    this test asserts on; the clean-by-default summary path is covered in the
    chat unit tests and the first-time-setup user journey.
    """
    config = make_config(send_envelope=True)
    msg = ChatMessage(kind="status", status="success", text="build green")

    send_webhook(config, msg)

    payloads = webhook_payloads()
    assert len(payloads) == 1
    text = payloads[0]["text"]
    # Summary line carries the status emoji + text.
    assert text.splitlines()[0] == f"{STATUS_EMOJI['success']} build green"
    # Body carries the JSON envelope with the frozen timestamp.
    envelope = json.loads(text.split("```")[1])
    assert envelope["kind"] == "status"
    assert envelope["status"] == "success"
    assert envelope["ts"] == frozen_clock
    assert envelope["version"] == "1"


def test_send_webhook_raises_on_http_500(make_config, mocked_webhook) -> None:
    """A non-2xx webhook response fails fast with a redacted (no-secret) URL."""
    import responses as responses_lib

    config = make_config()
    mocked_webhook.reset()
    mocked_webhook.add(responses_lib.POST, config.webhook_url, json={}, status=500)

    with pytest.raises(requests.HTTPError) as exc_info:
        send_webhook(config, ChatMessage(kind="status", status="info", text="ping"))

    message = str(exc_info.value)
    assert "500" in message
    # The query string carries the webhook secret; it must be redacted away.
    assert "TEST_TOKEN" not in message
    assert "TEST_KEY" not in message


# --------------------------------------------------------------------------- #
# Listener loop wired to the real chat REST helper + FakeChatService.
# --------------------------------------------------------------------------- #


def test_listener_emits_trigger_through_real_chat_helpers(
    make_config,
    fake_chat_service,
    human_trigger_message,
    monkeypatch,
) -> None:
    """A trigger message flows fetch -> parse -> emit via the real chat helper."""
    config = make_config()
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]
    monkeypatch.setattr(chat_module, "_build_service", lambda cfg: fake_chat_service)

    emitted = list(Listener(config).iter_new_messages(once=True))

    assert len(emitted) == 1
    assert emitted[0].kind == "command"
    assert emitted[0].command == "deploy"
    # The real list_messages issued one list against the configured space.
    assert fake_chat_service.list_calls[0]["parent"] == config.space_id


def test_listener_ignores_bot_message_in_catch_all(
    make_config,
    fake_chat_service,
    bot_trigger_message,
    monkeypatch,
) -> None:
    """A self (BOT) message surfaces nothing through the real helpers (loop guard)."""
    config = make_config(require_trigger=False)
    fake_chat_service.list_pages = [{"messages": [bot_trigger_message]}]
    monkeypatch.setattr(chat_module, "_build_service", lambda cfg: fake_chat_service)

    assert list(Listener(config).iter_new_messages(once=True)) == []


def test_listener_paginates_list_messages(
    make_config,
    fake_chat_service,
    make_raw_message,
    monkeypatch,
) -> None:
    """list_messages walks list_next pages; both pages' triggers are emitted."""
    config = make_config()
    page1 = make_raw_message(
        name="spaces/AAAA/messages/p1",
        text=f"{DEFAULT_TRIGGER_PREFIX} one",
        create_time="2026-06-20T00:00:01Z",
        thread=None,
    )
    page2 = make_raw_message(
        name="spaces/AAAA/messages/p2",
        text=f"{DEFAULT_TRIGGER_PREFIX} two",
        create_time="2026-06-20T00:00:02Z",
        thread=None,
    )
    fake_chat_service.list_pages = [{"messages": [page1]}, {"messages": [page2]}]
    monkeypatch.setattr(chat_module, "_build_service", lambda cfg: fake_chat_service)

    emitted = list(Listener(config).iter_new_messages(once=True))

    assert [m.command for m in emitted] == ["one", "two"]


def test_listener_filter_uses_since_after_first_poll(
    make_config,
    fake_chat_service,
    human_trigger_message,
    monkeypatch,
) -> None:
    """The second poll passes a createTime filter so only newer messages return."""
    config = make_config()
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]
    monkeypatch.setattr(chat_module, "_build_service", lambda cfg: fake_chat_service)

    listener = Listener(config)
    list(listener.iter_new_messages(once=True))
    # First list call has no since filter.
    assert "filter" not in fake_chat_service.list_calls[0]

    # Reset the page cursor for a fresh poll; the loop must now filter by since.
    fake_chat_service._page_cursor = 0
    list(listener.iter_new_messages(once=True))
    second = fake_chat_service.list_calls[-1]
    assert "filter" in second
    assert human_trigger_message["createTime"] in second["filter"]
