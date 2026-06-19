"""Google Chat transport: webhook send + Chat REST API read/delete.

Outbound status pings go through the configured incoming webhook (no OAuth).
Reading and deleting messages use the Chat REST API with OAuth credentials.
All network failures fail fast with the HTTP status and a redacted URL.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import requests

from claude_google_chat.auth import load_credentials
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, format_message, parse_message

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

# Chat space resource ids look like ``spaces/AAAA...``.
_SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")

_WEBHOOK_TIMEOUT_SECONDS = 30


def _redact_url(url: str) -> str:
    """Strip query parameters (which carry the webhook secret) from a URL."""
    return url.split("?", 1)[0]


def _require_space(config: Config) -> str:
    """Return a validated space id, raising on absence or bad format."""
    if not config.space_id:
        raise ValueError(
            "missing required config value 'space_id' (set CGC_SPACE_ID or add it to config.toml)"
        )
    if not _SPACE_RE.match(config.space_id):
        raise ValueError(f"invalid space id {config.space_id!r}; expected form 'spaces/<id>'")
    return config.space_id


def send_webhook(config: Config, msg: ChatMessage) -> None:
    """POST a formatted message to the configured incoming webhook.

    Raises ``ValueError`` if the webhook is unconfigured and
    ``requests.HTTPError`` (with a redacted URL) on any non-2xx response.
    """
    if not config.webhook_url:
        raise ValueError(
            "missing required config value 'webhook_url' "
            "(set CGC_WEBHOOK_URL or add it to config.toml)"
        )

    payload = {"text": format_message(msg)}
    response = requests.post(
        config.webhook_url,
        json=payload,
        timeout=_WEBHOOK_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise requests.HTTPError(
            f"webhook POST to {_redact_url(config.webhook_url)} failed with "
            f"status {response.status_code}"
        )


def _build_service(config: Config) -> Resource:
    """Build an authenticated Google Chat API client."""
    from googleapiclient.discovery import build

    creds = load_credentials(config)
    return build("chat", "v1", credentials=creds, cache_discovery=False)


def list_messages(
    config: Config,
    since: str | None = None,
    *,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """List messages in the configured space, optionally filtered by time.

    Args:
        config: Resolved configuration.
        since: Optional RFC3339 lower bound; messages with ``createTime``
            strictly greater are returned.
        page_size: API page size.

    Returns:
        A list of raw Chat API message resources.
    """
    space = _require_space(config)
    service = _build_service(config)

    request_kwargs: dict[str, Any] = {"parent": space, "pageSize": page_size}
    if since is not None:
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
