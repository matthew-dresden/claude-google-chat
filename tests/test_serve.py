"""Tests for the ``cgc serve`` responder loop.

The loop is driven through a bounded number of controlled iterations. External
boundaries are isolated two ways:

- **Injected callables** (``fetcher``/``poster``) exercise pure loop behavior —
  owner filtering, self-reply suppression, trigger gating, dedup, threading and
  the idle timeout — without any transport.
- The **FakeChatService** fixture is wired through the real
  ``chat.list_messages_as_app`` / ``chat.post_message_as_app`` code paths via
  the ``service=`` injection point, proving the responder posts a reply through
  the actual Chat-API request shape (no ``googleapiclient.discovery.build``).

The frozen clock keeps reply timestamps deterministic; a stub sleeper and a
scripted monotonic clock keep continuous-loop timing real-wait-free.
"""

from __future__ import annotations

from typing import Any

import pytest

from claude_google_chat import chat as chat_module
from claude_google_chat.config import Config
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, ChatMessage, parse_message
from claude_google_chat.serve import Responder, ServeTimeout, default_responder


def _config(**overrides: Any) -> Config:
    """Build a serve Config; input-driven via overrides."""
    base: dict[str, Any] = {
        "service_account_file": "/tmp/sa.json",
        "space_id": "spaces/AAAA",
        "trigger_prefix": DEFAULT_TRIGGER_PREFIX,
    }
    base.update(overrides)
    return Config(**base)


# --------------------------------------------------------------------------- #
# default_responder (pure).
# --------------------------------------------------------------------------- #


def test_default_responder_acknowledges_command() -> None:
    inbound = parse_message(f"{DEFAULT_TRIGGER_PREFIX} deploy prod")
    reply = default_responder(inbound)
    assert reply.kind == "result"
    assert reply.status == "success"
    assert "deploy" in reply.text


def test_default_responder_carries_correlation_id() -> None:
    inbound = ChatMessage(kind="command", command="ship", correlation_id="xyz")
    reply = default_responder(inbound)
    assert reply.correlation_id == "xyz"


# --------------------------------------------------------------------------- #
# Loop behavior via injected callables.
# --------------------------------------------------------------------------- #


def test_responds_once_to_owner_trigger(
    frozen_clock: str,
    human_trigger_message: dict[str, Any],
) -> None:
    config = _config(owner_email="owner@example.com")
    posted: list[tuple[ChatMessage, str | None]] = []

    responder = Responder(
        config,
        fetcher=lambda since: [human_trigger_message],
        poster=lambda reply, thread: posted.append((reply, thread)),
    )
    result = responder.run(once=True)

    assert len(result) == 1
    assert len(posted) == 1
    assert posted[0][0].kind == "result"
    assert "deploy" in posted[0][0].text


def test_ignores_non_owner_messages(
    frozen_clock: str,
    non_owner_trigger_message: dict[str, Any],
) -> None:
    config = _config(owner_email="owner@example.com")
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: [non_owner_trigger_message],
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_ignores_app_messages_to_avoid_self_reply(
    frozen_clock: str,
    bot_trigger_message: dict[str, Any],
) -> None:
    config = _config()
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: [bot_trigger_message],
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_ignores_non_trigger_messages(
    frozen_clock: str,
    human_plain_message: dict[str, Any],
) -> None:
    config = _config()
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: [human_plain_message],
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_does_not_reprocess_seen_messages(
    frozen_clock: str,
    human_trigger_message: dict[str, Any],
) -> None:
    config = _config()
    post_count = 0

    def poster(reply: ChatMessage, thread: str | None) -> None:
        nonlocal post_count
        post_count += 1

    responder = Responder(
        config,
        fetcher=lambda since: [human_trigger_message],
        poster=poster,
    )
    # Two drains: the second returns the same message, which must be deduped.
    responder.run(once=True)
    responder.run(once=True)
    assert post_count == 1


def test_reply_is_threaded_under_triggering_message(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    config = _config()
    triggering = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy",
        email="owner@x.com",
        thread="spaces/AAAA/threads/T1",
    )
    captured: list[str | None] = []
    responder = Responder(
        config,
        fetcher=lambda since: [triggering],
        poster=lambda reply, thread: captured.append(thread),
    )
    responder.run(once=True)
    assert captured == ["spaces/AAAA/threads/T1"]


def test_custom_responder_can_stay_silent(
    frozen_clock: str,
    human_trigger_message: dict[str, Any],
) -> None:
    config = _config()
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        responder=lambda inbound: None,
        fetcher=lambda since: [human_trigger_message],
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_multiple_owner_messages_in_one_poll_each_get_a_reply(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """A single poll batch with two owner triggers yields two posted replies."""
    config = _config(owner_email="owner@example.com")
    batch = [
        make_raw_message(
            name="spaces/AAAA/messages/1",
            text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
            email="owner@example.com",
            create_time="2026-06-20T12:00:00Z",
        ),
        make_raw_message(
            name="spaces/AAAA/messages/2",
            text=f"{DEFAULT_TRIGGER_PREFIX} rollback staging",
            email="owner@example.com",
            create_time="2026-06-20T12:00:01Z",
        ),
    ]
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: batch,
        poster=lambda reply, thread: posted.append(reply),
    )
    result = responder.run(once=True)

    assert len(result) == 2
    # default_responder summarises by parsed command name.
    assert [r.text for r in posted] == ["received: deploy", "received: rollback"]


# --------------------------------------------------------------------------- #
# Continuous loop timing / idle timeout.
# --------------------------------------------------------------------------- #


def test_idle_timeout_fails_fast(frozen_clock: str) -> None:
    config = _config(listen_timeout=10.0, poll_interval=1.0)
    clock_values = iter([0.0, 0.0, 11.0, 11.0])
    responder = Responder(
        config,
        fetcher=lambda since: [],
        poster=lambda reply, thread: None,
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: None,
    )
    with pytest.raises(ServeTimeout) as exc_info:
        responder.run(once=False)
    message = str(exc_info.value)
    assert "idle" in message
    assert "CGC_LISTEN_TIMEOUT" in message


def test_cadence_sleeper_paces_continuous_polls(frozen_clock: str) -> None:
    """The configured poll interval is used to pace continuous polling."""
    config = _config(listen_timeout=10.0, poll_interval=3.0)
    sleeps: list[float] = []
    clock_values = iter([0.0, 0.0, 0.0, 50.0, 50.0])
    responder = Responder(
        config,
        fetcher=lambda since: [],
        poster=lambda reply, thread: None,
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: sleeps.append(seconds),
    )
    with pytest.raises(ServeTimeout):
        responder.run(once=False)
    assert sleeps
    assert all(s == 3.0 for s in sleeps)


# --------------------------------------------------------------------------- #
# Real Chat-API transport via the FakeChatService injection point.
# --------------------------------------------------------------------------- #


def test_run_posts_reply_through_real_chat_api_shape(
    frozen_clock: str,
    fake_chat_service: Any,
    make_raw_message: Any,
) -> None:
    """The default fetcher/poster route through chat.py using the fake service.

    This exercises ``list_messages_as_app`` and ``post_message_as_app`` against
    the chained-builder fake (no discovery build), verifying the responder
    posts a reply via the real request shape and threads it under the trigger.
    """
    config = _config(owner_email="owner@example.com")
    triggering = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        email="owner@example.com",
        thread="spaces/AAAA/threads/T1",
    )
    fake_chat_service.list_pages = [{"messages": [triggering]}]

    def post(reply: ChatMessage, thread: str | None) -> None:
        chat_module.post_message_as_app(config, reply, service=fake_chat_service, thread_key=thread)

    responder = Responder(
        config,
        fetcher=lambda since: chat_module.list_messages_as_app(
            config, since=since, service=fake_chat_service
        ),
        poster=post,
    )
    posted = responder.run(once=True)

    assert len(posted) == 1
    # One Chat-API list call and one create call were made through the fake.
    assert len(fake_chat_service.list_calls) >= 1
    assert len(fake_chat_service.create_calls) == 1
    create_kwargs = fake_chat_service.create_calls[0]
    assert create_kwargs["parent"] == "spaces/AAAA"
    assert "received: deploy" in create_kwargs["body"]["text"]
    # Threaded under the triggering message's thread key.
    assert create_kwargs["body"]["thread"]["threadKey"] == "spaces/AAAA/threads/T1"
    assert create_kwargs["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def test_list_call_applies_since_filter_through_fake_service(
    frozen_clock: str,
    fake_chat_service: Any,
    make_raw_message: Any,
) -> None:
    """Across two iterations the second list call carries a createTime filter."""
    config = _config(owner_email="owner@example.com")
    first = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        email="owner@example.com",
        create_time="2026-06-20T12:00:00Z",
    )
    fake_chat_service.list_pages = [{"messages": [first]}]

    def post(reply: ChatMessage, thread: str | None) -> None:
        chat_module.post_message_as_app(config, reply, service=fake_chat_service, thread_key=thread)

    responder = Responder(
        config,
        fetcher=lambda since: chat_module.list_messages_as_app(
            config, since=since, service=fake_chat_service
        ),
        poster=post,
    )
    responder.run(once=True)
    # Empty subsequent page for the second drain.
    fake_chat_service.list_pages = [{"messages": []}]
    fake_chat_service._page_cursor = 0
    responder.run(once=True)

    first_list = fake_chat_service.list_calls[0]
    second_list = fake_chat_service.list_calls[-1]
    assert "filter" not in first_list
    assert second_list["filter"] == 'createTime > "2026-06-20T12:00:00Z"'
