"""Always-listening Google Chat responder (the ``cgc serve`` side-server).

This generalizes the proven internal responder approach into one reliable
loop: it polls the configured space as the **Chat app** (service-account auth),
and for every *new owner* message it invokes a responder and posts the reply
back to the space via the Chat API (threaded under the triggering message).

Design properties (consistent with ``listener.py``):

- **One always-listening loop**, **one responder per message**.
- Configurable ``trigger_prefix``, ``poll_interval`` (env-driven cadence, not a
  readiness ``sleep``) and ``listen_timeout`` (idle timeout → fail-fast exit).
- **Structured replies** built from :class:`ChatMessage` and the shared format.
- Secrets/config stay out of code: everything comes from ``Config``.

Only the owner (``owner_email``) triggers responses, so the app never replies to
its own posts or to other members. The default responder is structured and
side-effect-free (it acknowledges the parsed command); callers can inject a
richer responder.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.messages import (
    ChatMessage,
    format_message,
    parse_message,
)

# A responder maps an inbound (parsed) command message to an outbound reply, or
# None to stay silent for that message.
ResponderFn = Callable[[ChatMessage], "ChatMessage | None"]

# A poster delivers a reply ChatMessage, threaded under ``thread_key``.
Poster = Callable[[ChatMessage, "str | None"], None]

# A fetcher returns raw Chat API message resources newer than ``since``.
Fetcher = Callable[[str | None], list[dict[str, Any]]]


class ServeTimeout(RuntimeError):
    """Raised when the responder exceeds its configured idle timeout."""


def default_responder(message: ChatMessage) -> ChatMessage:
    """Return a structured acknowledgement reply for an inbound command.

    Pure (no I/O); used as the default per-message responder so ``cgc serve``
    is useful out of the box and unit-testable. The reply correlates back to the
    inbound message via ``correlation_id`` when present.
    """
    summary = message.command or message.text or "command"
    return ChatMessage(
        kind="result",
        status="success",
        text=f"received: {summary}",
        correlation_id=message.correlation_id,
    )


def _message_sender_email(raw: dict[str, Any]) -> str | None:
    """Extract the sender's email from a raw Chat message resource, if present."""
    sender = raw.get("sender")
    if isinstance(sender, dict):
        email = sender.get("email")
        if isinstance(email, str) and email:
            return email
    return None


def _is_app_message(raw: dict[str, Any]) -> bool:
    """Return True if the message was sent by a bot/app (not a human)."""
    sender = raw.get("sender")
    if isinstance(sender, dict):
        return sender.get("type") == "BOT"
    return False


def _thread_key(raw: dict[str, Any]) -> str | None:
    """Return the message's thread name so replies stay in-thread, if present."""
    thread = raw.get("thread")
    if isinstance(thread, dict):
        name = thread.get("name")
        if isinstance(name, str) and name:
            return name
    return None


class Responder:
    """Polls a Chat space as the app and responds to new owner messages."""

    def __init__(
        self,
        config: Config,
        *,
        responder: ResponderFn = default_responder,
        fetcher: Fetcher | None = None,
        poster: Poster | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the responder loop.

        Args:
            config: Resolved configuration (service-account auth + space).
            responder: Per-message handler producing a reply (or ``None`` to
                stay silent). Injectable so richer handlers can be supplied.
            fetcher: Returns raw Chat messages newer than ``since`` using app
                auth. Defaults to :func:`chat.list_messages_as_app`. Injectable
                for tests (no network).
            poster: Posts a reply threaded under a key. Defaults to
                :func:`chat.post_message_as_app`. Injectable for tests.
            clock: Monotonic clock source (injectable for tests).
            sleeper: Cadence sleeper between polls (injectable; paces polling,
                not a readiness wait).
        """
        self._config = config
        self._responder = responder
        self._fetcher = fetcher or self._default_fetcher
        self._poster = poster or self._default_poster
        self._clock = clock
        self._sleeper = sleeper
        self._seen: set[str] = set()
        self._since: str | None = None

    def _default_fetcher(self, since: str | None) -> list[dict[str, Any]]:
        from claude_google_chat.chat import list_messages_as_app

        return list_messages_as_app(self._config, since=since)

    def _default_poster(self, reply: ChatMessage, thread_key: str | None) -> None:
        from claude_google_chat.chat import post_message_as_app

        post_message_as_app(self._config, reply, thread_key=thread_key)

    def _should_handle(self, raw: dict[str, Any]) -> bool:
        """Return True if ``raw`` is a new, owner-authored trigger message."""
        if _is_app_message(raw):
            return False
        text = raw.get("text", "")
        if not text.strip().startswith(self._config.trigger_prefix):
            return False
        owner = self._config.owner_email
        if owner:
            sender = _message_sender_email(raw)
            if sender is None or sender.lower() != owner.lower():
                return False
        return True

    def _poll_once(self) -> list[ChatMessage]:
        """Fetch new messages, handle owner triggers, return replies posted."""
        raw_messages = self._fetcher(self._since)
        replies: list[ChatMessage] = []
        for raw in raw_messages:
            name = raw.get("name", "")
            if name and name in self._seen:
                continue
            if name:
                self._seen.add(name)
            create_time = raw.get("createTime")
            if create_time and (self._since is None or create_time > self._since):
                self._since = create_time
            if not self._should_handle(raw):
                continue
            inbound = parse_message(raw.get("text", ""), trigger_prefix=self._config.trigger_prefix)
            reply = self._responder(inbound)
            if reply is None:
                continue
            self._poster(reply, _thread_key(raw))
            replies.append(reply)
        return replies

    def run(self, *, once: bool = False) -> list[ChatMessage]:
        """Run the responder loop until idle timeout (or once when ``once``).

        Returns the list of replies posted (useful for ``--once`` and tests).

        Raises:
            ServeTimeout: if ``listen_timeout`` is > 0 and no owner message is
                handled within that idle window (fail fast).
        """
        posted: list[ChatMessage] = []
        last_activity = self._clock()
        while True:
            batch = self._poll_once()
            if batch:
                last_activity = self._clock()
                posted.extend(batch)
                for reply in batch:
                    sys.stdout.write(json.dumps(asdict(reply), sort_keys=True) + "\n")
                    sys.stdout.flush()

            if once:
                return posted

            timeout = self._config.listen_timeout
            if timeout > 0 and (self._clock() - last_activity) >= timeout:
                raise ServeTimeout(
                    f"no Google Chat owner messages handled within {timeout}s idle "
                    "timeout (CGC_LISTEN_TIMEOUT); exiting"
                )
            self._sleeper(self._config.poll_interval)


def run(config: Config, *, once: bool = False) -> int:
    """Run the serve loop; return a process exit code.

    Returns 0 on a clean ``--once`` drain, and non-zero on idle-timeout
    expiry (fail fast with a clear diagnostic on stderr).
    """
    responder = Responder(config)
    try:
        responder.run(once=once)
    except ServeTimeout as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


# Re-export so callers can format replies without importing messages directly.
__all__ = [
    "Responder",
    "ServeTimeout",
    "default_responder",
    "format_message",
    "run",
]
