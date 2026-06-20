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

import time
from collections.abc import Callable
from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.messages import (
    ChatMessage,
    format_message,
    parse_message,
)
from claude_google_chat.polling import PollLoop, run_to_exit_code

# A responder maps an inbound (parsed) command message to an outbound reply, or
# None to stay silent for that message.
ResponderFn = Callable[[ChatMessage], "ChatMessage | None"]

# A poster delivers a reply ChatMessage, threaded under ``thread_key``.
Poster = Callable[[ChatMessage, "str | None"], None]

# A fetcher returns raw Chat API message resources newer than ``since``.
Fetcher = Callable[[str | None], list[dict[str, Any]]]


class ServeTimeout(RuntimeError):
    """Raised when the responder exceeds its configured idle timeout."""


def _timeout_message(timeout: float) -> str:
    return (
        f"no Google Chat owner messages handled within {timeout}s idle "
        "timeout (CGC_LISTEN_TIMEOUT); exiting"
    )


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
        self._loop = PollLoop(
            config,
            fetcher=self._fetcher,
            handler=self._handle,
            timeout_exc=ServeTimeout,
            timeout_message=_timeout_message,
            clock=clock,
            sleeper=sleeper,
        )

    @property
    def _since(self) -> str | None:
        """Expose the shared poll cursor (highest ``createTime`` seen)."""
        return self._loop.since

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

    def _handle(self, raw: dict[str, Any]) -> ChatMessage | None:
        """Respond to a new owner trigger message, posting and returning the reply.

        Returns the reply :class:`ChatMessage` posted, or ``None`` when the
        message is not an owner trigger or the responder stays silent. The
        dedup/high-water bookkeeping is handled by the shared poll loop.
        """
        if not self._should_handle(raw):
            return None
        inbound = parse_message(raw.get("text", ""), trigger_prefix=self._config.trigger_prefix)
        reply = self._responder(inbound)
        if reply is None:
            return None
        self._poster(reply, _thread_key(raw))
        return reply

    def run(self, *, once: bool = False) -> list[ChatMessage]:
        """Run the responder loop until idle timeout (or once when ``once``).

        Returns the list of replies posted (useful for ``--once`` and tests).
        Each posted reply is emitted as one JSON line to stdout.

        Raises:
            ServeTimeout: if ``listen_timeout`` is > 0 and no owner message is
                handled within that idle window (fail fast).
        """
        return self._loop.run(once=once)


def run(config: Config, *, once: bool = False) -> int:
    """Run the serve loop; return a process exit code.

    Returns 0 on a clean ``--once`` drain, and non-zero on idle-timeout
    expiry (fail fast with a clear diagnostic on stderr).
    """
    responder = Responder(config)
    return run_to_exit_code(lambda: responder.run(once=once), ServeTimeout)


# Re-export so callers can format replies without importing messages directly.
__all__ = [
    "Responder",
    "ServeTimeout",
    "default_responder",
    "format_message",
    "run",
]
