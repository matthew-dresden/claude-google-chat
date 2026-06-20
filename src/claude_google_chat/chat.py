"""Google Chat transport: webhook send + Chat REST API read/delete.

Outbound status pings go through the configured incoming webhook (no OAuth).
Reading and deleting messages use the Chat REST API with OAuth credentials.
All network failures fail fast with the HTTP status and a redacted URL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests

from claude_google_chat.auth import load_app_credentials, load_credentials
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, format_message, parse_message
from claude_google_chat.validation import validate_create_time, validate_space_id

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource


def _redact_url(url: str) -> str:
    """Strip query parameters (which carry the webhook secret) from a URL."""
    return url.split("?", 1)[0]


def _require_space(config: Config) -> str:
    """Return a validated space id, raising on absence or bad format.

    Reuses :meth:`Config.require_keys` for the missing-value message (single
    source of truth, including the env-var hint) and the shared
    :func:`validate_space_id` for the format check.
    """
    config.require_keys(("space_id",))
    assert config.space_id is not None  # require_keys guarantees a non-empty value
    return validate_space_id(config.space_id)


def send_webhook(config: Config, msg: ChatMessage) -> None:
    """POST a formatted message to the configured incoming webhook.

    Raises ``ValueError`` if the webhook is unconfigured and
    ``requests.HTTPError`` (with a redacted URL) on any non-2xx response.
    The HTTP timeout is config-driven (``webhook_timeout`` / ``CGC_WEBHOOK_TIMEOUT``).
    """
    config.require_keys(("webhook_url",))
    assert config.webhook_url is not None  # require_keys guarantees a non-empty value

    payload = {"text": format_message(msg)}
    response = requests.post(
        config.webhook_url,
        json=payload,
        timeout=config.webhook_timeout,
    )
    if not response.ok:
        raise requests.HTTPError(
            f"webhook POST to {_redact_url(config.webhook_url)} failed with "
            f"status {response.status_code}"
        )


def _build_chat_service(config: Config, *, load_creds: Callable[[Config], object]) -> Resource:
    """Build a Google Chat API client using credentials from ``load_creds``.

    Single builder parameterised by the credentials loader so the user-OAuth and
    service-account (app) client constructions share one code path (DRY).
    """
    from googleapiclient.discovery import build

    creds = load_creds(config)
    return build("chat", "v1", credentials=creds, cache_discovery=False)


def _build_service(config: Config) -> Resource:
    """Build a Google Chat API client using cached user OAuth credentials."""
    return _build_chat_service(config, load_creds=load_credentials)


def build_app_service(config: Config) -> Resource:
    """Build a Google Chat API client using **service-account (app)** creds.

    Used by ``cgc serve`` so the process reads and posts messages as the Chat
    app itself rather than as a human user.
    """
    return _build_chat_service(config, load_creds=load_app_credentials)


def post_message_as_app(
    config: Config,
    msg: ChatMessage,
    *,
    service: Resource | None = None,
    thread_key: str | None = None,
) -> dict[str, Any]:
    """Post a formatted :class:`ChatMessage` to the space via the Chat API.

    Sends as the Chat app using service-account credentials (no webhook). When
    ``thread_key`` is provided the reply is threaded under that key so a
    response stays attached to the triggering message. Returns the created
    message resource. ``service`` is injectable for tests.
    """
    space = _require_space(config)
    chat = service if service is not None else build_app_service(config)
    body: dict[str, Any] = {"text": format_message(msg)}
    request_kwargs: dict[str, Any] = {"parent": space, "body": body}
    if thread_key is not None:
        body["thread"] = {"threadKey": thread_key}
        request_kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    return chat.spaces().messages().create(**request_kwargs).execute()


def _list_messages(
    service: Resource,
    space: str,
    since: str | None,
    page_size: int,
) -> list[dict[str, Any]]:
    """Paginate ``spaces.messages.list`` for ``space`` (shared by both readers).

    Holds the single pagination loop and ``createTime`` filter construction so
    the user-OAuth and app-auth readers do not duplicate it (DRY). ``since`` is
    validated against the RFC3339 shape before being interpolated into the Chat
    API ``filter`` expression so a malformed value fails fast.
    """
    request_kwargs: dict[str, Any] = {"parent": space, "pageSize": page_size}
    if since is not None:
        validate_create_time(since)
        request_kwargs["filter"] = f'createTime > "{since}"'

    messages: list[dict[str, Any]] = []
    request = service.spaces().messages().list(**request_kwargs)
    while request is not None:
        result = request.execute()
        messages.extend(result.get("messages", []))
        request = (
            service.spaces()
            .messages()
            .list_next(previous_request=request, previous_response=result)
        )
    return messages


def list_messages_as_app(
    config: Config,
    since: str | None = None,
    *,
    service: Resource | None = None,
) -> list[dict[str, Any]]:
    """List messages in the space using **service-account (app)** credentials.

    Mirrors :func:`list_messages` but authenticates as the Chat app. ``service``
    is injectable for tests so no network access is required. The page size is
    config-driven (``page_size`` / ``CGC_PAGE_SIZE``).
    """
    space = _require_space(config)
    chat = service if service is not None else build_app_service(config)
    return _list_messages(chat, space, since, config.page_size)


def list_messages(
    config: Config,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """List messages in the configured space, optionally filtered by time.

    Args:
        config: Resolved configuration.
        since: Optional RFC3339 lower bound; messages with ``createTime``
            strictly greater are returned.

    Returns:
        A list of raw Chat API message resources. The page size is config-driven
        (``page_size`` / ``CGC_PAGE_SIZE``).
    """
    space = _require_space(config)
    service = _build_service(config)
    return _list_messages(service, space, since, config.page_size)


def delete_message(config: Config, message_name: str) -> None:
    """Delete a message by its resource name via the Chat API."""
    if not message_name:
        raise ValueError("message_name must be a non-empty Chat message resource name")
    service = _build_service(config)
    service.spaces().messages().delete(name=message_name).execute()


def parse_chat_message(config: Config, raw: dict[str, Any]) -> ChatMessage:
    """Parse a raw Chat API message resource into a :class:`ChatMessage`."""
    text = raw.get("text", "")
    return parse_message(text, trigger_prefix=config.trigger_prefix)
