"""Event/poll-driven listener for inbound Google Chat messages.

The listener polls the configured space on an env-driven cadence
(``poll_interval``) and yields newly-seen messages whose text starts with the
configured trigger prefix. When one or more ``threads`` are configured it
further restricts emission to messages in those threads. The poll interval is a
documented cadence, not a readiness ``sleep``; an idle ``listen_timeout`` (when
> 0) causes a fail-fast non-zero exit with a clear diagnostic. Each emitted
message is written to stdout as a single JSON line (12-factor logs) and carries
the owning ``thread_name``.

The dedup/high-water bookkeeping, idle-timeout run loop, and stdout JSON-line
emission are provided by the shared :class:`claude_google_chat.polling.PollLoop`
so this module differs from the responder only in its per-message predicate.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from claude_google_chat.chat import list_messages
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, message_from_human_text, parse_message
from claude_google_chat.polling import PollLoop, run_to_exit_code
from claude_google_chat.rawmessage import is_human_message, thread_name
from claude_google_chat.state import FileStateStore, StateStore


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
        state_store: StateStore | None = None,
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
            state_store: Durable high-water store so a restart resumes instead
                of re-emitting recent history. Injectable for tests; the CLI
                supplies a file-backed store.
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
            state_store=state_store,
        )

    def _handle(self, raw: dict[str, Any]) -> ChatMessage | None:
        """Decide whether and how to emit a raw Chat message.

        With ``require_trigger`` set (the default), only messages whose text
        starts with the trigger prefix are emitted, parsed as structured
        commands. With ``require_trigger`` cleared (catch-all mode), **every**
        message from a HUMAN sender is surfaced — a trigger-prefixed line still
        parses as a command, while a plain conversational line is surfaced via
        :func:`message_from_human_text`. Non-human senders (BOT/app/webhook) are
        never surfaced in either mode, so the listener never echoes its own
        outbound posts or other bots (loop prevention).

        When one or more ``threads`` are configured, a message is emitted only
        when its raw ``thread.name`` is in that set — this filter composes with
        (is applied *in addition to*) the trigger / sender-type logic above.
        With no threads configured the whole space is surfaced (unchanged). The
        owning ``thread.name`` is always carried on the emitted message so a
        consumer knows which thread it belongs to.
        """
        raw_thread = thread_name(raw)
        threads = self._config.threads
        if threads and raw_thread not in threads:
            return None

        prefix = self._config.trigger_prefix
        text = raw.get("text", "")
        if self._config.require_trigger:
            if not text.strip().startswith(prefix):
                return None
            emitted = parse_message(text, trigger_prefix=prefix)
        else:
            if not is_human_message(raw):
                return None
            emitted = message_from_human_text(text, trigger_prefix=prefix)
        return replace(emitted, thread_name=raw_thread)

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


def _state_store(config: Config) -> StateStore:
    """Build the durable file-backed high-water store from ``config.state_file``."""
    from pathlib import Path

    assert config.state_file is not None  # always resolved by Config.load
    return FileStateStore(Path(config.state_file))


def run(config: Config, *, once: bool = False) -> int:
    """Run the listener, emitting one JSON line per message to stdout.

    Returns a process exit code: 0 on clean completion (``--once`` drain), and
    non-zero on idle timeout or consecutive-error exhaustion (fail fast with a
    clear diagnostic on stderr). The high-water cursor is persisted to
    ``state_file`` so a restart resumes instead of re-emitting recent history.
    """
    listener = Listener(config, state_store=_state_store(config))
    return run_to_exit_code(lambda: listener.run_to_stdout(once=once), ListenerTimeout)
