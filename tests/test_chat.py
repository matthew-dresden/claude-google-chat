"""Unit tests for the Google Chat transport (``chat.py``).

Every external boundary is isolated:

- ``send_webhook`` exercises the real ``requests.post`` path against the
  ``responses``-mocked incoming webhook (fixture ``mocked_webhook``), including
  the non-2xx fail-fast branch.
- ``list_messages`` / ``delete_message`` build a user-OAuth service internally;
  ``chat._build_service`` is monkeypatched to the injected
  :class:`FakeChatService` (fixture ``fake_chat_service``) so the OAuth/token
  path, the discovery ``build``, and any network call are never touched.
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
from tests.conftest import SPACE_ID

# --------------------------------------------------------------------------- #
# send_webhook (real requests path, mocked HTTP).
# --------------------------------------------------------------------------- #


def test_send_webhook_posts_clean_summary_by_default(
    make_config: Any,
    mocked_webhook: Any,
    webhook_payloads: Any,
    frozen_clock: str,
) -> None:
    """With the default ``send_envelope=False`` the webhook gets only the summary."""
    config = make_config()
    assert config.send_envelope is False
    msg = ChatMessage(kind="status", status="success", text="deploy done")

    chat.send_webhook(config, msg)

    payloads = webhook_payloads()
    assert len(payloads) == 1
    # Clean human view: the emoji-prefixed summary alone, no fenced JSON envelope.
    assert payloads[0] == {"text": format_message(msg, include_envelope=False)}
    assert "deploy done" in payloads[0]["text"]
    assert "```" not in payloads[0]["text"]


def test_send_webhook_posts_envelope_when_send_envelope_enabled(
    make_config: Any,
    mocked_webhook: Any,
    webhook_payloads: Any,
    frozen_clock: str,
) -> None:
    """``send_envelope=True`` appends the machine-readable JSON envelope."""
    config = make_config(send_envelope=True)
    msg = ChatMessage(kind="status", status="success", text="deploy done")

    chat.send_webhook(config, msg)

    payloads = webhook_payloads()
    assert len(payloads) == 1
    # The posted body carries the exact wire form produced by format_message.
    assert payloads[0] == {"text": format_message(msg, include_envelope=True)}
    assert "deploy done" in payloads[0]["text"]
    assert frozen_clock in payloads[0]["text"]
    assert "```" in payloads[0]["text"]


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
# send_webhook thread routing (--thread-key path).
# --------------------------------------------------------------------------- #


def test_send_webhook_without_thread_key_omits_thread_and_returns_none(
    make_config: Any,
    mocked_webhook: Any,
    webhook_payloads: Any,
    frozen_clock: str,
) -> None:
    """The non-threaded path is unchanged: no thread body, no reply option, None."""
    config = make_config()
    msg = ChatMessage(kind="status", status="success", text="hi")

    result = chat.send_webhook(config, msg)

    assert result is None
    payloads = webhook_payloads()
    assert len(payloads) == 1
    assert "thread" not in payloads[0]
    # The reply-option query param must not be appended on the unthreaded path.
    posted_url = mocked_webhook.calls[0].request.url
    assert "messageReplyOption" not in posted_url


def test_send_webhook_with_thread_key_adds_reply_option_and_thread_key(
    make_config: Any,
    mocked_webhook: Any,
    webhook_payloads: Any,
    frozen_clock: str,
) -> None:
    """A thread key appends the reply-option param and sets thread.threadKey."""
    mocked_webhook.reset()
    from tests.conftest import WEBHOOK_URL

    created_thread = f"{SPACE_ID}/threads/T-created"
    mocked_webhook.add(
        "POST",
        f"{WEBHOOK_URL}&{chat.THREAD_REPLY_OPTION}",
        json={"name": f"{SPACE_ID}/messages/m1", "thread": {"name": created_thread}},
        status=200,
    )

    config = make_config()
    msg = ChatMessage(kind="status", status="working", text="deploying")

    result = chat.send_webhook(config, msg, thread_key="deploy-42")

    # The created thread.name is returned for read-filtering.
    assert result == created_thread
    payloads = webhook_payloads()
    assert payloads[0]["thread"] == {"threadKey": "deploy-42"}
    posted_url = mocked_webhook.calls[0].request.url
    assert chat.THREAD_REPLY_OPTION in posted_url


def test_send_webhook_threaded_returns_none_when_response_has_no_thread(
    make_config: Any,
    mocked_webhook: Any,
    frozen_clock: str,
) -> None:
    """A threaded send whose response carries no thread.name returns None."""
    mocked_webhook.reset()
    from tests.conftest import WEBHOOK_URL

    mocked_webhook.add(
        "POST",
        f"{WEBHOOK_URL}&{chat.THREAD_REPLY_OPTION}",
        json={"name": f"{SPACE_ID}/messages/m2"},
        status=200,
    )
    config = make_config()
    msg = ChatMessage(kind="status", status="info", text="x")

    assert chat.send_webhook(config, msg, thread_key="k1") is None


def test_send_webhook_threaded_non_2xx_redacts_url(
    make_config: Any,
    mocked_webhook: Any,
    frozen_clock: str,
) -> None:
    """A failing threaded send fails fast with a redacted (secret-free) URL."""
    mocked_webhook.reset()
    from tests.conftest import WEBHOOK_URL

    mocked_webhook.add("POST", f"{WEBHOOK_URL}&{chat.THREAD_REPLY_OPTION}", status=500)
    config = make_config()
    msg = ChatMessage(kind="status", status="error", text="boom")

    with pytest.raises(requests.HTTPError) as exc_info:
        chat.send_webhook(config, msg, thread_key="k1")

    message = str(exc_info.value)
    assert "500" in message
    assert "TEST_KEY" not in message
    assert "TEST_TOKEN" not in message
    assert "messageReplyOption" not in message


# --------------------------------------------------------------------------- #
# list_messages (user-OAuth path via monkeypatched builder): pagination,
# time filter, space validation, error propagation.
# --------------------------------------------------------------------------- #


def test_list_messages_returns_messages(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
    human_trigger_message: dict[str, Any],
) -> None:
    fake_chat_service.list_pages = [{"messages": [human_trigger_message]}]
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()

    result = chat.list_messages(config)

    assert result == [human_trigger_message]
    # The space id and default page size were forwarded; no filter without since.
    assert fake_chat_service.list_calls[0]["parent"] == config.space_id
    assert fake_chat_service.list_calls[0]["pageSize"] == 100
    assert "filter" not in fake_chat_service.list_calls[0]


def test_list_messages_applies_since_filter(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()
    since = "2026-06-20T00:00:00Z"

    chat.list_messages(config, since=since)

    assert fake_chat_service.list_calls[0]["filter"] == f'createTime > "{since}"'


def test_list_messages_rejects_malformed_since(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    """A non-RFC3339 ``since`` fails fast instead of being injected into filter."""
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()
    with pytest.raises(ValueError) as exc_info:
        chat.list_messages(config, since='2026" OR createTime > "1970')
    assert "createTime" in str(exc_info.value)
    # No request was issued with the malformed value.
    assert fake_chat_service.list_calls == []


def test_list_messages_paginates(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
    make_raw_message: Any,
) -> None:
    first = make_raw_message(name="spaces/AAAA/messages/p1")
    second = make_raw_message(name="spaces/AAAA/messages/p2")
    # Two queued pages: list_next must walk to the second before terminating.
    fake_chat_service.list_pages = [{"messages": [first]}, {"messages": [second]}]
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()

    result = chat.list_messages(config)

    assert result == [first, second]


def test_list_messages_propagates_http_error(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    fake_chat_service: Any,
) -> None:
    # The fake's ``list`` wraps ``list_pages[0]`` in a _FakeExecutable, whose
    # execute() raises when the queued value is an Exception. Queue the error as
    # the first page to model an HttpError surfacing on execution.
    fake_chat_service.list_pages = [_http_error(403, b"PERMISSION_DENIED")]
    monkeypatch.setattr(chat, "_build_service", lambda _config: fake_chat_service)
    config = make_config()

    with pytest.raises(HttpError):
        chat.list_messages(config)


def test_list_messages_requires_space(
    make_config: Any,
) -> None:
    config = make_config(space_id=None)
    with pytest.raises(ValueError) as exc_info:
        chat.list_messages(config)
    assert "space_id" in str(exc_info.value)


def test_list_messages_rejects_malformed_space(
    make_config: Any,
) -> None:
    config = make_config(space_id="AAAA")
    with pytest.raises(ValueError) as exc_info:
        chat.list_messages(config)
    assert "spaces/" in str(exc_info.value)


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
    # listener's job, asserted in test_listener.py.)
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
