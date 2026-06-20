"""Shared pytest fixtures for the claude-google-chat test suite.

These fixtures isolate every external boundary the package touches so tests run
offline, deterministically, and without any real credentials:

- **Config files** live under a per-test ``tmp_path`` config dir (never the real
  OS config directory).
- The **Google Chat API ``service``** is a programmable fake that mirrors the
  ``googleapiclient`` chained-builder shape (``spaces().messages().list()`` /
  ``create()`` / ``delete()`` / ``list_next()``) so ``chat.py`` can be driven
  without network access.
- The **incoming webhook POST** is intercepted with the ``responses`` library so
  ``chat.send_webhook`` exercises the real ``requests`` call path.
- A **frozen clock** (``freezegun``) pins ``messages._now_rfc3339`` so timestamps
  in formatted envelopes are deterministic and assertable.
- **Sample payloads** model HUMAN and BOT senders, with and without the trigger
  prefix, threaded and unthreaded, matching real Chat ``messages.list`` items.

Everything is input-driven: factories accept overrides so a test supplies only
what it asserts on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import responses as responses_lib
from freezegun import freeze_time

from claude_google_chat.config import Config
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX

# --------------------------------------------------------------------------- #
# Deterministic constants (single source of truth for the suite).
# --------------------------------------------------------------------------- #

FROZEN_INSTANT = "2026-06-20T12:00:00Z"
SPACE_ID = "spaces/AAAA"
SENDER_EMAIL = "owner@example.com"
WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=TEST_KEY&token=TEST_TOKEN"


# --------------------------------------------------------------------------- #
# Existing data dir fixture (kept for backward compatibility with file fixtures).
# --------------------------------------------------------------------------- #


@pytest.fixture
def data_dir() -> Path:
    """Return the path to the static test data directory."""
    return Path(__file__).parent / "data"


# --------------------------------------------------------------------------- #
# Frozen clock.
# --------------------------------------------------------------------------- #


@pytest.fixture
def frozen_clock() -> Iterator[str]:
    """Freeze wall-clock time so RFC3339 timestamps are deterministic.

    ``messages._now_rfc3339`` calls ``datetime.now(UTC)``; freezegun patches it
    so ``format_message`` and trigger-line parsing emit ``FROZEN_INSTANT``.
    Yields the frozen instant string for direct assertions.
    """
    with freeze_time(FROZEN_INSTANT):
        yield FROZEN_INSTANT


# --------------------------------------------------------------------------- #
# Temp config directory + config file.
# --------------------------------------------------------------------------- #


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Return an isolated, writable config directory under ``tmp_path``."""
    path = tmp_path / "cgc-config"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def config_path(config_dir: Path) -> Path:
    """Return the path to a (not-yet-written) ``config.toml`` in the temp dir."""
    return config_dir / "config.toml"


@pytest.fixture
def write_config_file(config_path: Path) -> Callable[..., Path]:
    """Return a factory that writes a ``config.toml`` from keyword values.

    Values are serialised as minimal TOML literals. The file is written with
    owner-only (0600) permissions to mirror production behavior. Returns the
    written path so the test can load it via ``Config.load(path=...)``.
    """

    def _toml_literal(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _write(**values: object) -> Path:
        lines = ["# test config", ""]
        for key, value in values.items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_literal(value)}")
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config_path.chmod(0o600)
        return config_path

    return _write


@pytest.fixture
def make_config() -> Callable[..., Config]:
    """Return a factory producing a realistic in-memory :class:`Config`.

    Defaults cover the common user-OAuth + space + webhook setup; pass overrides
    (or ``None`` to clear a field) for the specific path under test. No file or
    environment I/O — input-driven for fast, isolated unit tests.
    """

    def _make(**overrides: Any) -> Config:
        base: dict[str, Any] = {
            "webhook_url": WEBHOOK_URL,
            "space_id": SPACE_ID,
            "oauth_client_file": "/tmp/client_secret.json",
            "token_file": "/tmp/token.json",
            "trigger_prefix": DEFAULT_TRIGGER_PREFIX,
        }
        base.update(overrides)
        return Config(**base)

    return _make


# --------------------------------------------------------------------------- #
# Sample Chat message payloads (raw messages.list resources).
# --------------------------------------------------------------------------- #


def _build_raw_message(
    *,
    name: str,
    text: str,
    sender_type: str,
    email: str | None,
    create_time: str,
    thread: str | None,
) -> dict[str, Any]:
    """Construct a raw Chat API message resource matching ``messages.list``."""
    sender: dict[str, Any] = {"type": sender_type}
    if email is not None:
        sender["email"] = email
    raw: dict[str, Any] = {
        "name": name,
        "text": text,
        "createTime": create_time,
        "sender": sender,
    }
    if thread is not None:
        raw["thread"] = {"name": thread}
    return raw


@pytest.fixture
def make_raw_message() -> Callable[..., dict[str, Any]]:
    """Return a factory building raw Chat message resources.

    Mirrors the shape returned by ``spaces.messages.list``. Defaults produce a
    HUMAN-sent, trigger-prefixed, threaded message; override any field for the
    case under test (e.g. ``sender_type="BOT"`` or a non-trigger ``text``).
    """

    def _make(
        *,
        name: str = f"{SPACE_ID}/messages/1",
        text: str = f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        sender_type: str = "HUMAN",
        email: str | None = SENDER_EMAIL,
        create_time: str = FROZEN_INSTANT,
        thread: str | None = f"{SPACE_ID}/threads/T1",
    ) -> dict[str, Any]:
        return _build_raw_message(
            name=name,
            text=text,
            sender_type=sender_type,
            email=email,
            create_time=create_time,
            thread=thread,
        )

    return _make


@pytest.fixture
def human_trigger_message(make_raw_message: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """A HUMAN message that starts with the trigger prefix (should be emitted)."""
    return make_raw_message(
        name=f"{SPACE_ID}/messages/human-1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        sender_type="HUMAN",
        email=SENDER_EMAIL,
    )


@pytest.fixture
def human_plain_message(make_raw_message: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """A HUMAN message without the trigger prefix (should be ignored)."""
    return make_raw_message(
        name=f"{SPACE_ID}/messages/human-2",
        text="just chatting, no command here",
        sender_type="HUMAN",
        email=SENDER_EMAIL,
        thread=None,
    )


@pytest.fixture
def bot_trigger_message(make_raw_message: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """A BOT/app message with the trigger prefix (must be ignored: self-reply)."""
    return make_raw_message(
        name=f"{SPACE_ID}/messages/bot-1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        sender_type="BOT",
        email=None,
        thread=None,
    )


# --------------------------------------------------------------------------- #
# Fake Google Chat API discovery ``service``.
# --------------------------------------------------------------------------- #


class _FakeExecutable:
    """A request stand-in whose ``execute()`` returns a queued result or raises."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeMessages:
    """Fake of ``service.spaces().messages()`` supporting list/create/delete."""

    def __init__(self, service: FakeChatService) -> None:
        self._service = service

    def list(self, **kwargs: Any) -> _FakeExecutable:
        self._service.list_calls.append(kwargs)
        pages = self._service.list_pages
        page = pages[0] if pages else {"messages": []}
        return _FakeExecutable(page)

    def list_next(self, previous_request: Any, previous_response: Any) -> Any:
        # Walk to the next queued page; ``None`` terminates pagination.
        idx = self._service._page_cursor + 1
        self._service._page_cursor = idx
        if idx < len(self._service.list_pages):
            self._service.pending_next = _FakeExecutable(self._service.list_pages[idx])
            return self._service.pending_next
        return None

    def create(self, **kwargs: Any) -> _FakeExecutable:
        self._service.create_calls.append(kwargs)
        if self._service.create_error is not None:
            return _FakeExecutable(self._service.create_error)
        return _FakeExecutable(self._service.create_result)

    def delete(self, **kwargs: Any) -> _FakeExecutable:
        self._service.delete_calls.append(kwargs)
        if self._service.delete_error is not None:
            return _FakeExecutable(self._service.delete_error)
        return _FakeExecutable({})


class _FakeSpaces:
    """Fake of ``service.spaces()`` exposing messages."""

    def __init__(self, service: FakeChatService) -> None:
        self._service = service

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self._service)


class FakeChatService:
    """Programmable stand-in for a built googleapiclient Chat ``Resource``.

    Mirrors the chained-builder access patterns used by ``chat.py``:
    ``svc.spaces().messages().list(...).execute()`` and ``.list_next(...)``,
    ``.create(...)``, and ``.delete(...)``.

    Configure return values / errors via the public attributes and assert on the
    recorded ``*_calls`` lists. Errors are raised from ``execute()`` to model the
    real client (where ``HttpError`` surfaces on execution).
    """

    def __init__(self) -> None:
        # Paginated list results: a list of ``{"messages": [...]}`` page dicts.
        self.list_pages: list[dict[str, Any]] = [{"messages": []}]
        self._page_cursor = 0
        self.pending_next: _FakeExecutable | None = None

        self.create_result: dict[str, Any] = {"name": f"{SPACE_ID}/messages/created-1"}
        self.create_error: Exception | None = None

        self.delete_error: Exception | None = None

        # Recorded calls for assertions.
        self.list_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def spaces(self) -> _FakeSpaces:
        return _FakeSpaces(self)


@pytest.fixture
def fake_chat_service() -> FakeChatService:
    """Return a fresh, programmable fake Chat API ``service``.

    Inject it by monkeypatching ``chat._build_service`` so no real discovery
    build or network call occurs.
    """
    return FakeChatService()


# --------------------------------------------------------------------------- #
# Mocked incoming webhook POST.
# --------------------------------------------------------------------------- #


@pytest.fixture
def mocked_webhook() -> Iterator[responses_lib.RequestsMock]:
    """Intercept the incoming-webhook POST with a 200 OK by default.

    Yields the active ``responses`` mock so a test can override the registered
    response (e.g. register a 500 to assert ``send_webhook`` fails fast) and
    inspect ``mock.calls`` to verify the posted JSON envelope. Exercises the real
    ``requests.post`` code path in ``chat.send_webhook``.
    """
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(
            responses_lib.POST,
            WEBHOOK_URL,
            json={},
            status=200,
        )
        yield mock


@pytest.fixture
def webhook_payloads(mocked_webhook: responses_lib.RequestsMock) -> Callable[[], list[Any]]:
    """Return a callable yielding the decoded JSON bodies POSTed to the webhook.

    Convenience over ``mocked_webhook.calls`` for asserting on the message text
    that ``send_webhook`` transmitted.
    """

    def _payloads() -> list[Any]:
        bodies: list[Any] = []
        for call in mocked_webhook.calls:
            body = call.request.body
            if body is None:
                continue
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            bodies.append(json.loads(body))
        return bodies

    return _payloads
