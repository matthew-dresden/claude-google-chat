"""Tests for the serve responder loop (message parsing, owner filtering, replies).

Network is fully injected via fake fetcher/poster callables, so these exercise
real loop behavior without touching Google APIs.
"""

from __future__ import annotations

from typing import Any

import pytest

from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, parse_message
from claude_google_chat.serve import Responder, ServeTimeout, default_responder


def _msg(
    name: str,
    text: str,
    *,
    email: str | None = None,
    bot: bool = False,
    create_time: str = "2026-06-20T00:00:00Z",
    thread: str | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {"name": name, "text": text, "createTime": create_time}
    sender: dict[str, Any] = {}
    if bot:
        sender["type"] = "BOT"
    else:
        sender["type"] = "HUMAN"
    if email is not None:
        sender["email"] = email
    raw["sender"] = sender
    if thread is not None:
        raw["thread"] = {"name": thread}
    return raw


def _config(**overrides: Any) -> Config:
    base = {
        "service_account_file": "/tmp/sa.json",
        "space_id": "spaces/AAAA",
        "trigger_prefix": "claude-command:",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def test_default_responder_acknowledges_command() -> None:
    inbound = parse_message("claude-command: deploy prod")
    reply = default_responder(inbound)
    assert reply.kind == "result"
    assert reply.status == "success"
    assert "deploy" in reply.text


def test_default_responder_carries_correlation_id() -> None:
    inbound = ChatMessage(kind="command", command="ship", correlation_id="xyz")
    reply = default_responder(inbound)
    assert reply.correlation_id == "xyz"


def test_responds_once_to_owner_trigger() -> None:
    config = _config(owner_email="owner@example.com")
    fetched = [
        _msg("spaces/AAAA/messages/1", "claude-command: deploy", email="owner@example.com"),
    ]
    posted: list[tuple[ChatMessage, str | None]] = []

    responder = Responder(
        config,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: posted.append((reply, thread)),
    )
    result = responder.run(once=True)

    assert len(result) == 1
    assert len(posted) == 1
    assert posted[0][0].kind == "result"


def test_ignores_non_owner_messages() -> None:
    config = _config(owner_email="owner@example.com")
    fetched = [
        _msg("spaces/AAAA/messages/1", "claude-command: deploy", email="intruder@example.com"),
    ]
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_ignores_app_messages_to_avoid_self_reply() -> None:
    config = _config()
    fetched = [
        _msg("spaces/AAAA/messages/1", "claude-command: deploy", bot=True),
    ]
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_ignores_non_trigger_messages() -> None:
    config = _config()
    fetched = [_msg("spaces/AAAA/messages/1", "just chatting", email="owner@example.com")]
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []


def test_does_not_reprocess_seen_messages() -> None:
    config = _config()
    fetched = [_msg("spaces/AAAA/messages/1", "claude-command: deploy", email="o@x.com")]
    post_count = 0

    def poster(reply: ChatMessage, thread: str | None) -> None:
        nonlocal post_count
        post_count += 1

    responder = Responder(config, fetcher=lambda since: fetched, poster=poster)
    responder.run(once=True)
    responder.run(once=True)
    assert post_count == 1


def test_reply_is_threaded_under_triggering_message() -> None:
    config = _config()
    fetched = [
        _msg(
            "spaces/AAAA/messages/1",
            "claude-command: deploy",
            email="o@x.com",
            thread="spaces/AAAA/threads/T1",
        )
    ]
    captured: list[str | None] = []
    responder = Responder(
        config,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: captured.append(thread),
    )
    responder.run(once=True)
    assert captured == ["spaces/AAAA/threads/T1"]


def test_idle_timeout_fails_fast() -> None:
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
    assert "idle" in str(exc_info.value)


def test_custom_responder_can_stay_silent() -> None:
    config = _config()
    fetched = [_msg("spaces/AAAA/messages/1", "claude-command: noop", email="o@x.com")]
    posted: list[ChatMessage] = []
    responder = Responder(
        config,
        responder=lambda inbound: None,
        fetcher=lambda since: fetched,
        poster=lambda reply, thread: posted.append(reply),
    )
    assert responder.run(once=True) == []
    assert posted == []
