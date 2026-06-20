"""Event/poll-driven listener for inbound Google Chat messages.

The listener polls the configured space on an env-driven cadence
(``poll_interval``) and yields newly-seen messages whose text starts with the
configured trigger prefix. The poll interval is a documented cadence, not a
readiness ``sleep``; an idle ``listen_timeout`` (when > 0) causes a fail-fast
non-zero exit with a clear diagnostic. Each emitted message is written to
stdout as a single JSON line (12-factor logs).

The dedup/high-water bookkeeping, idle-timeout run loop, and stdout JSON-line
emission are provided by the shared :class:`claude_google_chat.polling.PollLoop`
so this module differs from the responder only in its per-message predicate.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

from claude_google_chat.chat import list_messages
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, parse_message
from claude_google_chat.polling import PollLoop, run_to_exit_code


class ListenerTimeout(RuntimeError):
    """Raised when the listener exceeds its configured idle timeout."""


def _timeout_message(timeout: float) -> str:
    return (
        f"no new Google Chat messages within {timeout}s idle timeout (CGC_LISTEN_TIMEOUT); exiting"
    )


class Listener:
    """Polls a Google Chat space and yields new trigger-prefixed messages."""

    def __init__(
        self,
        config: Config,
        *,
        fetcher: Callable[[Config, str | None], list[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the listener.

        Args:
            config: Resolved configuration.
            fetcher: Injectable function returning raw Chat messages (defaults
                to :func:`claude_google_chat.chat.list_messages`). Input-driven
                so tests can supply a fake transport.
            clock: Monotonic clock source (injectable for tests).
            sleeper: Cadence sleeper between polls (injectable for tests). This
                paces polling; it is not a readiness wait.
        """
        self._config = config
        resolved_fetcher = fetcher or (lambda cfg, since: list_messages(cfg, since=since))
        self._loop = PollLoop(
            config,
            fetcher=lambda since: resolved_fetcher(config, since),
            handler=self._handle,
            timeout_exc=ListenerTimeout,
            timeout_message=_timeout_message,
            clock=clock,
            sleeper=sleeper,
        )

    def _handle(self, raw: dict[str, Any]) -> ChatMessage | None:
        """Return a parsed message if its text starts with the trigger prefix."""
        prefix = self._config.trigger_prefix
        text = raw.get("text", "")
        if not text.strip().startswith(prefix):
            return None
        return parse_message(text, trigger_prefix=prefix)

    def iter_new_messages(self, *, once: bool = False) -> Iterator[ChatMessage]:
        """Yield newly-seen trigger-prefixed messages.

        Args:
            once: If true, drain currently-pending messages and stop (for hook
                or CI use). Otherwise poll continuously until the idle timeout
                (when configured) expires.

        Raises:
            ListenerTimeout: If ``listen_timeout`` is > 0 and no new message is
                seen within that window.
        """
        return self._loop.iter_emitted(once=once)

    def run_to_stdout(self, *, once: bool = False) -> list[ChatMessage]:
        """Emit each new message as one JSON line to stdout; return the list.

        Raises:
            ListenerTimeout: on idle-timeout expiry (fail fast).
        """
        return self._loop.run(once=once)


def run(config: Config, *, once: bool = False) -> int:
    """Run the listener, emitting one JSON line per message to stdout.

    Returns a process exit code: 0 on clean completion (``--once`` drain), and
    non-zero on idle timeout (fail fast with a clear diagnostic on stderr).
    """
    listener = Listener(config)
    return run_to_exit_code(lambda: listener.run_to_stdout(once=once), ListenerTimeout)
