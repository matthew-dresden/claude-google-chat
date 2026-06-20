"""Integration tests: real components wired together, only external boundaries mocked.

Unlike the unit tests (which inject fake ``fetcher``/``poster`` callables directly),
these exercise the *real* transport functions in :mod:`claude_google_chat.chat`
(``send_webhook``, ``list_messages_as_app``, ``post_message_as_app``) and the real
:class:`~claude_google_chat.serve.Responder` loop wired to them. The only things
stubbed are the two genuine external boundaries:

- the **incoming webhook HTTP POST** (the ``responses`` library, via ``mocked_webhook``),
  so the real ``requests.post`` path in ``send_webhook`` runs; and
- the **Google Chat REST API** (the ``FakeChatService`` fixture), injected through
  the existing ``service=`` parameter so ``list_messages_as_app`` /
  ``post_message_as_app`` run their real request-building / pagination code without a
  ``googleapiclient.discovery.build`` call.

Everything else (config, message parsing, formatting, owner filtering, dedup,
threading) is the production code.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from claude_google_chat.chat import (
    list_messages_as_app,
    post_message_as_app,
    send_webhook,
)
from claude_google_chat.messages import (
    DEFAULT_TRIGGER_PREFIX,
    STATUS_EMOJI,
    ChatMessage,
)
from claude_google_chat.serve import Responder

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
# Responder loop wired to the real chat REST helpers + FakeChatService.
# --------------------------------------------------------------------------- #


def _wire_responder(config, fake_chat_service, **kwargs: Any) -> Responder:
    """Build a Responder whose fetcher/poster are the *real* chat helpers.

    Only the googleapiclient service is faked (injected via ``service=``); the
    request building, pagination, owner filtering, and threading are production.
    """

    def _fetch(since: str | None) -> list[dict[str, Any]]:
        return list_messages_as_app(config, since=since, service=fake_chat_service)

    def _post(reply: ChatMessage, thread: str | None) -> None:
        # Discard the created-message resource: Poster's contract returns None.
        post_message_as_app(config, reply, service=fake_chat_service, thread_key=thread)

    return Responder(config, fetcher=_fetch, poster=_post, **kwargs)


def test_responder_handles_owner_trigger_through_real_chat_helpers(
    make_config,
    fake_chat_service,
    human_trigger_message,
) -> None:
    """An owner trigger flows fetch -> parse -> respond -> post via real helpers."""
    config = make_config()
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]

    responder = _wire_responder(config, fake_chat_service)
    replies = responder.run(once=True)

    assert len(replies) == 1
    assert replies[0].kind == "result"
    assert replies[0].status == "success"

    # The real post_message_as_app issued exactly one create against the space.
    assert len(fake_chat_service.create_calls) == 1
    create = fake_chat_service.create_calls[0]
    assert create["parent"] == config.space_id
    assert "received" in create["body"]["text"]
    # Threaded under the triggering message's thread.
    assert create["body"]["thread"]["threadKey"] == human_trigger_message["thread"]["name"]


def test_responder_ignores_bot_and_non_owner_through_real_helpers(
    make_config,
    fake_chat_service,
    bot_trigger_message,
    non_owner_trigger_message,
) -> None:
    """Self (BOT) and non-owner triggers post nothing through the real helpers."""
    config = make_config(owner_email="owner@example.com")
    fake_chat_service.list_pages = [{"messages": [bot_trigger_message, non_owner_trigger_message]}]

    responder = _wire_responder(config, fake_chat_service)
    assert responder.run(once=True) == []
    assert fake_chat_service.create_calls == []


def test_responder_paginates_list_messages(
    make_config,
    fake_chat_service,
    make_raw_message,
) -> None:
    """list_messages_as_app walks list_next pages; both pages' triggers handled."""
    config = make_config(owner_email=None)
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

    responder = _wire_responder(config, fake_chat_service)
    replies = responder.run(once=True)

    assert len(replies) == 2
    assert len(fake_chat_service.create_calls) == 2


def test_responder_filter_uses_since_after_first_poll(
    make_config,
    fake_chat_service,
    human_trigger_message,
) -> None:
    """The second poll passes a createTime filter so only newer messages return."""
    config = make_config(owner_email=None)
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]

    responder = _wire_responder(config, fake_chat_service)
    responder.run(once=True)
    # First list call has no since filter.
    assert "filter" not in fake_chat_service.list_calls[0]

    # Reset the page cursor for a fresh poll; the loop must now filter by since.
    fake_chat_service._page_cursor = 0
    responder.run(once=True)
    second = fake_chat_service.list_calls[-1]
    assert "filter" in second
    assert human_trigger_message["createTime"] in second["filter"]


def test_post_message_as_app_unthreaded_when_no_thread_key(
    make_config,
    fake_chat_service,
) -> None:
    """Without a thread key the created message carries no thread/reply option."""
    config = make_config()
    post_message_as_app(
        config,
        ChatMessage(kind="status", status="info", text="hello"),
        service=fake_chat_service,
    )
    create = fake_chat_service.create_calls[0]
    assert "thread" not in create["body"]
    assert "messageReplyOption" not in create
