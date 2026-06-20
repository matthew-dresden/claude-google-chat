"""Accessors for raw Google Chat ``messages.list`` resources.

A raw Chat message resource is a plain ``dict`` returned by the Chat REST API.
The listener needs the sender ``type`` out of it (to tell a HUMAN apart from a
BOT/app and avoid surfacing the listener's own outbound posts). These accessors
are the single source of truth for reaching into that shape (DRY) and are pure
(no I/O) so they unit-test in isolation.
"""

from __future__ import annotations

from typing import Any

HUMAN_SENDER_TYPE = "HUMAN"


def sender_type(raw: dict[str, Any]) -> str | None:
    """Return the raw message sender's ``type`` (e.g. ``HUMAN``/``BOT``), if present."""
    sender = raw.get("sender")
    if isinstance(sender, dict):
        value = sender.get("type")
        if isinstance(value, str) and value:
            return value
    return None


def is_human_message(raw: dict[str, Any]) -> bool:
    """Return ``True`` if the message was sent by a HUMAN (not a bot/app/webhook).

    Excluding non-human senders prevents the listener/responder from surfacing
    its own outbound posts or other bots' messages (loop prevention).
    """
    return sender_type(raw) == HUMAN_SENDER_TYPE
