"""Google Chat transport: webhook send + Chat REST API read/delete.

Outbound status pings go through the configured incoming webhook (no OAuth).
Reading and deleting messages use the Chat REST API with OAuth credentials.
All network failures fail fast with the HTTP status and a redacted URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import requests

from claude_google_chat.auth import load_credentials
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, format_message, parse_message
from claude_google_chat.rawmessage import thread_name
from claude_google_chat.validation import validate_create_time, validate_space_id

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

# Incoming-webhook query param that routes a post into a caller-keyed thread,
# starting a new thread when the key is unseen. Single source of truth.
THREAD_REPLY_OPTION = "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def _redact_url(url: str) -> str:
    """Strip query parameters (which carry the webhook secret) from a URL."""
    return url.split("?", 1)[0]


def _with_thread_reply_option(url: str) -> str:
    """Append the thread-reply query param to ``url`` (preserving existing params)."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{THREAD_REPLY_OPTION}"


def _require_space(config: Config) -> str:
    """Return a validated space id, raising on absence or bad format.

    Reuses :meth:`Config.require_keys` for the missing-value message (single
    source of truth, including the env-var hint) and the shared
    :func:`validate_space_id` for the format check.
    """
    config.require_keys(("space_id",))
    assert config.space_id is not None  # require_keys guarantees a non-empty value
    return validate_space_id(config.space_id)


def send_webhook(config: Config, msg: ChatMessage, thread_key: str | None = None) -> str | None:
    """POST a formatted message to the configured incoming webhook.

    When ``thread_key`` is given the post is routed into a caller-keyed thread:
    the :data:`THREAD_REPLY_OPTION` query param is appended to the webhook URL
    and ``thread.threadKey`` is set in the JSON body. Repeated posts with the
    same key land in the same thread; an unseen key starts a new thread. The
    webhook response is the created Message resource, from which the stable
    ``thread.name`` is extracted and returned so a caller can learn the
    thread.name for that key (needed for read-filtering). With no ``thread_key``
    behaviour is unchanged and ``None`` is returned.

    Raises ``ValueError`` if the webhook is unconfigured and
    ``requests.HTTPError`` (with a redacted URL) on any non-2xx response.
    The HTTP timeout is config-driven (``webhook_timeout`` / ``CGC_WEBHOOK_TIMEOUT``).
    """
    config.require_keys(("webhook_url",))
    assert config.webhook_url is not None  # require_keys guarantees a non-empty value

    url = config.webhook_url
    payload: dict[str, Any] = {"text": format_message(msg, include_envelope=config.send_envelope)}
    if thread_key is not None:
        url = _with_thread_reply_option(url)
        payload["thread"] = {"threadKey": thread_key}

    response = requests.post(
        url,
        json=payload,
        timeout=config.webhook_timeout,
    )
    if not response.ok:
        raise requests.HTTPError(
            f"webhook POST to {_redact_url(url)} failed with status {response.status_code}"
        )
    if thread_key is None:
        return None
    return thread_name(response.json())


def _build_service(config: Config) -> Resource:
    """Build a Google Chat API client using cached user OAuth credentials."""
    from googleapiclient.discovery import build

    creds = load_credentials(config)
    return build("chat", "v1", credentials=creds, cache_discovery=False)


def _list_messages(
    service: Resource,
    space: str,
    since: str | None,
    page_size: int,
) -> list[dict[str, Any]]:
    """Paginate ``spaces.messages.list`` for ``space``.

    Holds the single pagination loop and ``createTime`` filter construction.
    ``since`` is validated against the RFC3339 shape before being interpolated
    into the Chat API ``filter`` expression so a malformed value fails fast.
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
