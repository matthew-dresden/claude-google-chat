"""Tests for ``cgc bootstrap``: pure helpers plus the app-auth setup flow.

Two layers are covered:

- **Pure helpers** (topic normalization, subscription body, error
  classification) — no I/O.
- **The setup flow** — space join/create and the Workspace Events subscription
  — driven against the ``FakeChatService`` (and a fake events service) through
  the real ``bootstrap`` code paths. No ``googleapiclient.discovery.build``,
  OAuth, or network call is made: credential/service builders are monkeypatched
  and ``HttpError`` is raised from the fake's ``execute()`` to model the Chat API
  surfacing the manual-configuration gate (NOT_FOUND / PERMISSION_DENIED).

The key behavioral assertion is the **NOT_CONFIGURED gate**: when the Chat app
has not been configured in the console, every Chat API call fails, and bootstrap
must raise :class:`ChatAppNotConfiguredError` with exact, actionable steps rather
than leaking a raw stack trace.
"""

from __future__ import annotations

from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from claude_google_chat import bootstrap as bootstrap_module
from claude_google_chat.bootstrap import (
    MESSAGE_CREATED_EVENT,
    ChatAppNotConfiguredError,
    SpaceNotFoundError,
    _create_subscription,
    _ensure_space,
    bootstrap,
    build_subscription_body,
    is_not_configured_error,
    normalize_pubsub_topic,
)
from claude_google_chat.config import Config


def _http_error(status: int, message: str) -> HttpError:
    """Build a googleapiclient ``HttpError`` with a given status and message.

    Models the real client where ``HttpError`` surfaces on ``execute()`` and its
    ``str()`` embeds the server message (used by the not-configured classifier).
    """
    resp = Response({"status": status})
    content = ('{"error": {"message": "' + message + '"}}').encode("utf-8")
    return HttpError(resp, content, uri="https://chat.googleapis.com")


def _config(**overrides: Any) -> Config:
    """Build a bootstrap Config; input-driven via overrides."""
    base: dict[str, Any] = {
        "service_account_file": "/tmp/sa.json",
        "project_id": "test-project",
        "pubsub_topic": "projects/test-project/topics/chat-events",
        "space_id": "spaces/AAAA",
    }
    base.update(overrides)
    return Config(**base)


class _FakeEventsService:
    """Minimal fake of the Workspace Events ``subscriptions()`` chain.

    Models both ``create()`` (the happy/gate path) and ``list(filter=...)`` (the
    idempotent 409 path that fetches the real existing subscription name).
    """

    def __init__(
        self,
        *,
        result: Any = None,
        error: Exception | None = None,
        list_result: Any = None,
    ) -> None:
        self._result = result if result is not None else {"name": "subscriptions/sub-1"}
        self._error = error
        self._list_result = list_result if list_result is not None else {"subscriptions": []}
        self.create_bodies: list[dict[str, Any]] = []
        self.list_filters: list[str] = []
        self._pending: Any = None

    def subscriptions(self) -> _FakeEventsService:
        return self

    def create(self, *, body: dict[str, Any]) -> _FakeEventsService:
        self.create_bodies.append(body)
        self._pending = ("create", None)
        return self

    def list(self, *, filter: str) -> _FakeEventsService:
        # ``filter`` mirrors the Workspace Events API keyword exactly.
        self.list_filters.append(filter)
        self._pending = ("list", self._list_result)
        return self

    def execute(self) -> Any:
        kind, payload = self._pending
        if kind == "create":
            if self._error is not None:
                raise self._error
            return self._result
        return payload


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #


def test_normalize_topic_from_bare_id_and_project() -> None:
    assert normalize_pubsub_topic("my-proj", "chat-events") == "projects/my-proj/topics/chat-events"


def test_normalize_topic_passthrough_qualified() -> None:
    qualified = "projects/p1/topics/t1"
    assert normalize_pubsub_topic(None, qualified) == qualified


def test_normalize_topic_bare_id_requires_project() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_pubsub_topic(None, "chat-events")
    assert "project_id" in str(exc_info.value)


def test_normalize_topic_rejects_partial_path() -> None:
    with pytest.raises(ValueError):
        normalize_pubsub_topic("p1", "topics/t1")


def test_normalize_topic_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalize_pubsub_topic("p1", "")


def test_subscription_body_shape() -> None:
    body = build_subscription_body("spaces/AAAA", "projects/p/topics/t")
    assert body["targetResource"] == "//chat.googleapis.com/spaces/AAAA"
    assert body["eventTypes"] == [MESSAGE_CREATED_EVENT]
    assert body["notificationEndpoint"]["pubsubTopic"] == "projects/p/topics/t"
    assert body["payloadOptions"]["includeResource"] is True


def test_subscription_body_rejects_bad_space() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_subscription_body("AAAA", "projects/p/topics/t")
    assert "space" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "message",
    [
        "PERMISSION_DENIED: the caller does not have permission",
        "the Chat app is not configured",
    ],
)
def test_is_not_configured_error_detects_by_substring(message: str) -> None:
    """The unambiguous "not configured" phrasings classify without a status."""
    assert is_not_configured_error(message) is True


def test_is_not_configured_error_detects_403_status() -> None:
    """An HTTP 403 (PERMISSION_DENIED) is the pending-Configuration signal."""
    assert is_not_configured_error("any message", status=403) is True


def test_is_not_configured_error_404_is_not_misclassified() -> None:
    """A bare 404 NOT_FOUND is NOT the configuration gate (it's space-not-found)."""
    assert is_not_configured_error("404 NOT_FOUND", status=404) is False


def test_is_not_configured_error_ignores_unrelated() -> None:
    assert is_not_configured_error("INVALID_ARGUMENT: bad request body", status=400) is False


# --------------------------------------------------------------------------- #
# _ensure_space: join an existing space.
# --------------------------------------------------------------------------- #


def test_ensure_space_joins_existing_space(fake_chat_service: Any) -> None:
    """A configured space id triggers a members.create (join) call."""
    config = _config(space_id="spaces/AAAA")

    space_id, created, joined = _ensure_space(config, fake_chat_service)

    assert space_id == "spaces/AAAA"
    assert created is False
    assert joined is True
    assert len(fake_chat_service.member_create_calls) == 1
    call = fake_chat_service.member_create_calls[0]
    assert call["parent"] == "spaces/AAAA"
    assert call["body"]["member"]["type"] == "BOT"


def test_ensure_space_join_already_member_is_idempotent(fake_chat_service: Any) -> None:
    """A 409 on join is treated as idempotent success (already a member)."""
    config = _config(space_id="spaces/AAAA")
    fake_chat_service.member_create_error = _http_error(409, "ALREADY_EXISTS")

    space_id, created, joined = _ensure_space(config, fake_chat_service)

    assert space_id == "spaces/AAAA"
    assert created is False
    assert joined is False


# --------------------------------------------------------------------------- #
# _ensure_space: create a new space.
# --------------------------------------------------------------------------- #


def test_ensure_space_creates_new_space_from_display_name(fake_chat_service: Any) -> None:
    """With no space id but a display name, a new space is created."""
    config = _config(space_id=None, space_display_name="Ops Room")
    fake_chat_service.space_create_result = {"name": "spaces/NEW"}

    space_id, created, joined = _ensure_space(config, fake_chat_service)

    assert space_id == "spaces/NEW"
    assert created is True
    assert joined is False
    assert len(fake_chat_service.space_create_calls) == 1
    body = fake_chat_service.space_create_calls[0]["body"]
    assert body["displayName"] == "Ops Room"
    assert body["spaceType"] == "SPACE"


def test_ensure_space_requires_space_id_or_display_name(fake_chat_service: Any) -> None:
    """Neither a space id nor a display name fails fast with guidance."""
    config = _config(space_id=None, space_display_name=None)
    with pytest.raises(ValueError) as exc_info:
        _ensure_space(config, fake_chat_service)
    message = str(exc_info.value)
    assert "CGC_SPACE_ID" in message
    assert "CGC_SPACE_DISPLAY_NAME" in message


def test_ensure_space_created_without_name_fails_fast(fake_chat_service: Any) -> None:
    """A created space missing a resource name is a hard error (no fallback)."""
    config = _config(space_id=None, space_display_name="Ops Room")
    fake_chat_service.space_create_result = {}
    with pytest.raises(RuntimeError) as exc_info:
        _ensure_space(config, fake_chat_service)
    assert "resource name" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# NOT_CONFIGURED gate (the irreducible manual Chat-app Configuration step).
# --------------------------------------------------------------------------- #


def test_join_permission_denied_raises_configuration_gate(fake_chat_service: Any) -> None:
    """A 403 PERMISSION_DENIED on join surfaces the configuration gate."""
    config = _config(space_id="spaces/AAAA")
    fake_chat_service.member_create_error = _http_error(
        403, "PERMISSION_DENIED: caller does not have permission"
    )

    with pytest.raises(ChatAppNotConfiguredError) as exc_info:
        _ensure_space(config, fake_chat_service)

    message = str(exc_info.value)
    assert "not configured" in message
    assert "cgc bootstrap" in message
    assert "Configuration" in message
    # The original API error is preserved as the cause (no swallowing).
    assert isinstance(exc_info.value.__cause__, HttpError)


def test_join_not_found_raises_space_not_found(fake_chat_service: Any) -> None:
    """A 404 on a configured space id is reported as space-not-found, not the gate.

    A valid-but-nonexistent space id (operator typo) must get the accurate
    "space not found or app lacks access" remediation rather than the long
    "configure your Chat app" instructions.
    """
    config = _config(space_id="spaces/AAAA")
    fake_chat_service.member_create_error = _http_error(404, "NOT_FOUND")

    with pytest.raises(SpaceNotFoundError) as exc_info:
        _ensure_space(config, fake_chat_service)

    message = str(exc_info.value)
    assert "spaces/AAAA" in message
    assert "not found" in message.lower()
    # Must NOT leak the configuration-gate instructions for this root cause.
    assert "Configuration console" not in message
    assert isinstance(exc_info.value.__cause__, HttpError)


def test_create_space_not_configured_raises_actionable_error(fake_chat_service: Any) -> None:
    """The configuration gate also fires on space creation failures."""
    config = _config(space_id=None, space_display_name="Ops Room")
    fake_chat_service.space_create_error = _http_error(403, "PERMISSION_DENIED")

    with pytest.raises(ChatAppNotConfiguredError):
        _ensure_space(config, fake_chat_service)


def test_join_propagates_unrelated_http_error(fake_chat_service: Any) -> None:
    """A non-configuration HTTP error is not masked as a configuration problem."""
    config = _config(space_id="spaces/AAAA")
    fake_chat_service.member_create_error = _http_error(400, "INVALID_ARGUMENT: bad body")

    with pytest.raises(HttpError):
        _ensure_space(config, fake_chat_service)


# --------------------------------------------------------------------------- #
# _create_subscription (Workspace Events).
# --------------------------------------------------------------------------- #


def test_create_subscription_returns_name() -> None:
    config = _config()
    events = _FakeEventsService(result={"name": "subscriptions/sub-9"})

    name = _create_subscription(
        config, events, "spaces/AAAA", "projects/test-project/topics/chat-events"
    )

    assert name == "subscriptions/sub-9"
    assert len(events.create_bodies) == 1
    body = events.create_bodies[0]
    assert body["eventTypes"] == [MESSAGE_CREATED_EVENT]
    assert body["targetResource"] == "//chat.googleapis.com/spaces/AAAA"
    assert body["notificationEndpoint"]["pubsubTopic"] == "projects/test-project/topics/chat-events"


def test_create_subscription_idempotent_returns_real_name() -> None:
    """A 409 fetches and returns the *real* existing subscription resource name."""
    config = _config()
    events = _FakeEventsService(
        error=_http_error(409, "ALREADY_EXISTS"),
        list_result={"subscriptions": [{"name": "subscriptions/existing-7"}]},
    )

    name = _create_subscription(
        config, events, "spaces/AAAA", "projects/test-project/topics/chat-events"
    )

    assert name == "subscriptions/existing-7"
    # The lookup was filtered by the Chat space target resource.
    assert events.list_filters == ['target_resource="//chat.googleapis.com/spaces/AAAA"']


def test_create_subscription_idempotent_without_listed_name() -> None:
    """A 409 with no matching listing falls back to an explicit marker (not fabricated)."""
    config = _config()
    events = _FakeEventsService(
        error=_http_error(409, "ALREADY_EXISTS"),
        list_result={"subscriptions": []},
    )

    name = _create_subscription(
        config, events, "spaces/AAAA", "projects/test-project/topics/chat-events"
    )

    assert "spaces/AAAA" in name
    assert "name unavailable" in name


def test_create_subscription_not_configured_raises_actionable_error() -> None:
    config = _config()
    events = _FakeEventsService(error=_http_error(403, "PERMISSION_DENIED"))

    with pytest.raises(ChatAppNotConfiguredError):
        _create_subscription(
            config, events, "spaces/AAAA", "projects/test-project/topics/chat-events"
        )


# --------------------------------------------------------------------------- #
# Full bootstrap flow (services + config write monkeypatched).
# --------------------------------------------------------------------------- #


def test_bootstrap_requires_pubsub_topic() -> None:
    config = _config(pubsub_topic=None)
    with pytest.raises(ValueError) as exc_info:
        bootstrap(config)
    assert "pubsub_topic" in str(exc_info.value)


def test_bootstrap_joins_space_and_creates_subscription(
    monkeypatch: pytest.MonkeyPatch,
    fake_chat_service: Any,
) -> None:
    """End-to-end bootstrap: join space, subscribe, merge results into config."""
    config = _config(space_id="spaces/AAAA")
    events = _FakeEventsService(result={"name": "subscriptions/sub-1"})
    written: dict[str, Any] = {}

    monkeypatch.setattr(bootstrap_module, "_build_chat_service", lambda cfg: fake_chat_service)
    monkeypatch.setattr(bootstrap_module, "_build_events_service", lambda cfg: events)

    def fake_merge(updates: Any, path: Any = None) -> str:
        written.update(updates)
        return "/tmp/config.toml"

    monkeypatch.setattr(bootstrap_module, "merge_and_write_config", fake_merge)

    result = bootstrap(config)

    assert result.space_id == "spaces/AAAA"
    assert result.joined_space is True
    assert result.created_space is False
    assert result.subscription_name == "subscriptions/sub-1"
    assert result.pubsub_topic == "projects/test-project/topics/chat-events"
    # Discovered values are merged into config for subsequent ``cgc serve`` runs.
    assert written["space_id"] == "spaces/AAAA"
    assert written["pubsub_topic"] == "projects/test-project/topics/chat-events"
    # The events subscription targeted the joined space.
    assert events.create_bodies[0]["targetResource"] == "//chat.googleapis.com/spaces/AAAA"


def test_bootstrap_surfaces_not_configured_gate(
    monkeypatch: pytest.MonkeyPatch,
    fake_chat_service: Any,
) -> None:
    """If the Chat app is not configured, bootstrap raises the actionable gate."""
    config = _config(space_id="spaces/AAAA")
    fake_chat_service.member_create_error = _http_error(403, "PERMISSION_DENIED")
    events = _FakeEventsService()

    monkeypatch.setattr(bootstrap_module, "_build_chat_service", lambda cfg: fake_chat_service)
    monkeypatch.setattr(bootstrap_module, "_build_events_service", lambda cfg: events)
    monkeypatch.setattr(
        bootstrap_module, "merge_and_write_config", lambda updates, path=None: "/tmp/config.toml"
    )

    with pytest.raises(ChatAppNotConfiguredError) as exc_info:
        bootstrap(config)

    message = str(exc_info.value)
    assert "not configured" in message
    assert "cgc bootstrap" in message
    # No subscription was attempted because the space step failed fast.
    assert events.create_bodies == []
