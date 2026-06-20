"""Unit tests for the Google Chat transport (``chat.py``).

Every external boundary is isolated:

- ``send_webhook`` exercises the real ``requests.post`` path against the
  ``responses``-mocked incoming webhook (fixture ``mocked_webhook``), including
  the non-2xx fail-fast branch.
- ``list_messages_as_app`` / ``post_message_as_app`` are driven through the
  injected :class:`FakeChatService` (fixture ``fake_chat_service``) so no
  discovery ``build`` and no network call occur.
- ``list_messages`` / ``delete_message`` build a user-OAuth service internally;
  ``chat._build_service`` is monkeypatched to the fake so the OAuth/token path is
  never touched.
- HUMAN vs BOT (and trigger vs plain) classification is asserted via
  :func:`parse_chat_message` over the shared raw-message fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
from googleapiclient.errors import HttpError

from claude_google_chat import chat
from claude_google_chat.messages import ChatMessage, format_message

# --------------------------------------------------------------------------- #
# send_webhook (real requests path, mocked HTTP).
# --------------------------------------------------------------------------- #


def test_send_webhook_posts_formatted_envelope(
    make_config: Any,
    mocked_webhook: Any,
    webhook_payloads: Any,
    frozen_clock: str,
) -> None:
    config = make_config()
    msg = ChatMessage(kind="status", status="success", text="deploy done")

    chat.send_webhook(config, msg)

    payloads = webhook_payloads()
    assert len(payloads) == 1
    # The posted body carries the exact wire form produced by format_message.
    assert payloads[0] == {"text": format_message(msg)}
    assert "deploy done" in payloads[0]["text"]
    assert frozen_clock in payloads[0]["text"]


def test_send_webhook_requires_webhook_url(make_config: Any) -> None:
    config = make_config(webhook_url=None)
    msg = ChatMessage(kind="status", status="info", text="hi")

    with pytest.raises(ValueError) as exc_info:
        chat.send_webhook(config, msg)
    assert "webhook_url" in str(exc_info.value)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 503])
def test_send_webhook_raises_on_non_2xx(
    make_config: Any,
    mocked_webhook: Any,
    frozen_clock: str,
    status_code: int,
) -> None:
    # Override the default 200 registration with a failing status.
    mocked_webhook.reset()
    from tests.conftest import WEBHOOK_URL

    mocked_webhook.add("POST", WEBHOOK_URL, status=status_code)

    config = make_config()
    msg = ChatMessage(kind="status", status="error", text="boom")

    with pytest.raises(requests.HTTPError) as exc_info:
        chat.send_webhook(config, msg)

    message = str(exc_info.value)
    assert str(status_code) in message
    # The webhook secret (query string) must be redacted from the error.
    assert "TEST_KEY" not in message
    assert "TEST_TOKEN" not in message
    assert "key=" not in message


def test_send_webhook_2xx_non_200_is_accepted(
    make_config: Any,
    mocked_webhook: Any,
    frozen_clock: str,
) -> None:
    mocked_webhook.reset()
    from tests.conftest import WEBHOOK_URL

    mocked_webhook.add("POST", WEBHOOK_URL, status=204)

    config = make_config()
    msg = ChatMessage(kind="status", status="success", text="ok")

    # 204 is a 2xx response; send_webhook must not raise.
    chat.send_webhook(config, msg)


# --------------------------------------------------------------------------- #
# list_messages_as_app (injected fake service, pagination, time filter).
# --------------------------------------------------------------------------- #


def test_list_messages_as_app_returns_messages(
    make_config: Any,
    fake_chat_service: Any,
    human_trigger_message: dict[str, Any],
) -> None:
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]
    config = make_config()

    result = chat.list_messages_as_app(config, service=fake_chat_service)

    assert result == [human_trigger_message]
    # The space id and default page size were forwarded; no filter without since.
    assert fake_chat_service.list_calls[0]["parent"] == config.space_id
    assert fake_chat_service.list_calls[0]["pageSize"] == 100
    assert "filter" not in fake_chat_service.list_calls[0]


def test_list_messages_as_app_applies_since_filter(
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    config = make_config()
    since = "2026-06-20T00:00:00Z"

    chat.list_messages_as_app(config, since=since, service=fake_chat_service)

    assert fake_chat_service.list_calls[0]["filter"] == f'createTime > "{since}"'


def test_list_messages_as_app_paginates(
    make_config: Any,
    fake_chat_service: Any,
    make_raw_message: Any,
) -> None:
    first = make_raw_message(name="spaces/AAAA/messages/p1")
    second = make_raw_message(name="spaces/AAAA/messages/p2")
    # Two queued pages: list_next must walk to the second before terminating.
    fake_chat_service.list_pages = [{"messages": [first]}, {"messages": [second]}]
    config = make_config()

    result = chat.list_messages_as_app(config, service=fake_chat_service)

    assert result == [first, second]


def test_list_messages_as_app_propagates_http_error(
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    # The fake's ``list`` wraps ``list_pages[0]`` in a _FakeExecutable, whose
    # execute() raises when the queued value is an Exception. Queue the error as
    # the first page to model an HttpError surfacing on execution.
    fake_chat_service.list_pages = [_http_error(403, b"PERMISSION_DENIED")]
    config = make_config()

    with pytest.raises(HttpError):
        chat.list_messages_as_app(config, service=fake_chat_service)


def test_list_messages_as_app_requires_space(
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    config = make_config(space_id=None)
    with pytest.raises(ValueError) as exc_info:
        chat.list_messages_as_app(config, service=fake_chat_service)
    assert "space_id" in str(exc_info.value)


def test_list_messages_as_app_rejects_malformed_space(
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    config = make_config(space_id="AAAA")
    with pytest.raises(ValueError) as exc_info:
        chat.list_messages_as_app(config, service=fake_chat_service)
    assert "spaces/" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# post_message_as_app (injected fake service).
# --------------------------------------------------------------------------- #


def test_post_message_as_app_creates_message(
    make_config: Any,
    fake_chat_service: Any,
    frozen_clock: str,
) -> None:
    config = make_config()
    msg = ChatMessage(kind="result", status="success", text="all good")

    result = chat.post_message_as_app(config, msg, service=fake_chat_service)

    assert result == fake_chat_service.create_result
    call = fake_chat_service.create_calls[0]
    assert call["parent"] == config.space_id
    assert call["body"]["text"] == format_message(msg)
    # Unthreaded post: no reply option and no thread in the body.
    assert "messageReplyOption" not in call
    assert "thread" not in call["body"]


def test_post_message_as_app_threads_reply(
    make_config: Any,
    fake_chat_service: Any,
    frozen_clock: str,
) -> None:
    config = make_config()
    msg = ChatMessage(kind="result", status="success", text="reply")

    chat.post_message_as_app(config, msg, service=fake_chat_service, thread_key="T1")

    call = fake_chat_service.create_calls[0]
    assert call["body"]["thread"] == {"threadKey": "T1"}
    assert call["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def test_post_message_as_app_propagates_http_error(
    make_config: Any,
    fake_chat_service: Any,
    frozen_clock: str,
) -> None:
    fake_chat_service.create_error = _http_error(500, b"INTERNAL")
    config = make_config()
    msg = ChatMessage(kind="status", status="error", text="x")

    with pytest.raises(HttpError):
        chat.post_message_as_app(config, msg, service=fake_chat_service)


# --------------------------------------------------------------------------- #
# list_messages / delete_message (user-OAuth path via monkeypatched builder).
# --------------------------------------------------------------------------- #


def test_list_messages_uses_built_service(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
    human_trigger_message: dict[str, Any],
) -> None:
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()

    result = chat.list_messages(config, since="2026-06-20T00:00:00Z")

    assert result == [human_trigger_message]
    assert fake_chat_service.list_calls[0]["filter"] == 'createTime > "2026-06-20T00:00:00Z"'


def test_delete_message_calls_api(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()
    name = "spaces/AAAA/messages/to-delete"

    chat.delete_message(config, name)

    assert fake_chat_service.delete_calls == [{"name": name}]


def test_delete_message_rejects_empty_name(make_config: Any) -> None:
    config = make_config()
    with pytest.raises(ValueError) as exc_info:
        chat.delete_message(config, "")
    assert "message_name" in str(exc_info.value)


def test_delete_message_propagates_http_error(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    fake_chat_service.delete_error = _http_error(404, b"NOT_FOUND")
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()

    with pytest.raises(HttpError):
        chat.delete_message(config, "spaces/AAAA/messages/gone")


# --------------------------------------------------------------------------- #
# parse_chat_message: HUMAN trigger vs BOT / plain HUMAN classification.
# --------------------------------------------------------------------------- #


def test_parse_chat_message_parses_human_trigger(
    make_config: Any,
    human_trigger_message: dict[str, Any],
    frozen_clock: str,
) -> None:
    config = make_config()
    parsed = chat.parse_chat_message(config, human_trigger_message)

    assert parsed.kind == "command"
    assert parsed.command == "deploy"
    assert parsed.args == ["prod"]


def test_parse_chat_message_parses_bot_trigger_text(
    make_config: Any,
    bot_trigger_message: dict[str, Any],
    frozen_clock: str,
) -> None:
    # parse_chat_message looks only at text; the BOT sender carries the same
    # trigger text, so parsing succeeds. (Sender-type *filtering* is the
    # responder's job, asserted in test_serve.py.)
    config = make_config()
    parsed = chat.parse_chat_message(config, bot_trigger_message)

    assert parsed.kind == "command"
    assert parsed.command == "deploy"


def test_parse_chat_message_rejects_plain_human_text(
    make_config: Any,
    human_plain_message: dict[str, Any],
) -> None:
    config = make_config()
    with pytest.raises(ValueError):
        chat.parse_chat_message(config, human_plain_message)


def test_parse_chat_message_honors_custom_trigger_prefix(
    make_config: Any,
    make_raw_message: Any,
    frozen_clock: str,
) -> None:
    config = make_config(trigger_prefix="!run:")
    raw = make_raw_message(text="!run: ship it")
    parsed = chat.parse_chat_message(config, raw)

    assert parsed.command == "ship"
    assert parsed.args == ["it"]


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _http_error(status: int, content: bytes) -> HttpError:
    """Build a googleapiclient ``HttpError`` modeling an API failure response."""

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status = code
            self.reason = "error"

    return HttpError(resp=_Resp(status), content=content)
