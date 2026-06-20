"""Shared Google Chat polling primitive.

Both the inbound listener (``cgc listen``) and the always-listening responder
(``cgc serve``) poll the same space on an env-driven cadence and need identical
bookkeeping: per-message ``name`` dedup, ``createTime`` high-water tracking, an
idle-timeout-with-monotonic-clock run loop, and one-JSON-line-per-message stdout
emission (12-factor logs). This module holds that single implementation so the
two callers differ only in their per-message predicate and action (DRY).

The poll cadence is a documented, env-driven cadence (``poll_interval``), not a
readiness ``sleep``; the idle ``listen_timeout`` (when > 0) fails fast with a
clear, caller-supplied diagnostic.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, to_jsonl

# Maps a raw Chat message resource (already deduped + high-water-tracked) to an
# outbound :class:`ChatMessage` to emit, or ``None`` to skip it.
MessageHandler = Callable[[dict[str, Any]], "ChatMessage | None"]

# Returns the records newer than ``since`` (raw Chat message resources).
RawFetcher = Callable[[str | None], list[dict[str, Any]]]

# Builds the idle-timeout diagnostic message from the configured timeout value.
TimeoutMessage = Callable[[float], str]


class PollLoop:
    """Polls a Chat space, deduping and high-water-tracking, with idle timeout.

    Holds the ``_seen``/``_since`` cursor bookkeeping and the run loop shared by
    the listener and responder. Each poll calls the injected ``fetcher`` for raw
    messages newer than the cursor, applies dedup and ``createTime`` tracking,
    then defers the per-message decision/action to ``handler``.
    """

    def __init__(
        self,
        config: Config,
        *,
        fetcher: RawFetcher,
        handler: MessageHandler,
        timeout_exc: type[Exception],
        timeout_message: TimeoutMessage,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the poll loop.

        Args:
            config: Resolved configuration (cadence + idle timeout).
            fetcher: Returns raw Chat messages newer than ``since`` (injectable
                for tests so no network access is required).
            handler: Per-message predicate/action returning a :class:`ChatMessage`
                to emit, or ``None`` to skip.
            timeout_exc: Exception type raised on idle-timeout expiry (fail fast).
            timeout_message: Builds the diagnostic string for ``timeout_exc``.
            clock: Monotonic clock source (injectable for tests).
            sleeper: Cadence sleeper between polls (injectable; paces polling,
                not a readiness wait).
        """
        self._config = config
        self._fetcher = fetcher
        self._handler = handler
        self._timeout_exc = timeout_exc
        self._timeout_message = timeout_message
        self._clock = clock
        self._sleeper = sleeper
        self._seen: set[str] = set()
        self._since: str | None = None

    @property
    def since(self) -> str | None:
        """Highest ``createTime`` seen so far (the poll cursor)."""
        return self._since

    def poll_once(self) -> list[ChatMessage]:
        """Fetch new messages, dedup + high-water track, return handler outputs."""
        raw_messages = self._fetcher(self._since)
        emitted: list[ChatMessage] = []
        for raw in raw_messages:
            name = raw.get("name", "")
            if name and name in self._seen:
                continue
            if name:
                self._seen.add(name)
            create_time = raw.get("createTime")
            if create_time and (self._since is None or create_time > self._since):
                self._since = create_time
            result = self._handler(raw)
            if result is not None:
                emitted.append(result)
        return emitted

    def iter_emitted(self, *, once: bool = False) -> Iterator[ChatMessage]:
        """Yield each emitted message, polling on cadence with an idle timeout.

        Args:
            once: If true, drain currently-pending messages and stop.

        Raises:
            The configured ``timeout_exc`` if ``listen_timeout`` is > 0 and no
            message is emitted within that idle window (fail fast).
        """
        last_activity = self._clock()
        while True:
            batch = self.poll_once()
            if batch:
                last_activity = self._clock()
            yield from batch

            if once:
                return

            timeout = self._config.listen_timeout
            if timeout > 0 and (self._clock() - last_activity) >= timeout:
                raise self._timeout_exc(self._timeout_message(timeout))
            self._sleeper(self._config.poll_interval)

    def run(self, *, once: bool = False) -> list[ChatMessage]:
        """Drive :meth:`iter_emitted`, writing one JSON line per message to stdout.

        Returns the list of emitted messages (useful for ``--once`` and tests).
        Raises the configured ``timeout_exc`` on idle-timeout expiry.
        """
        emitted: list[ChatMessage] = []
        for msg in self.iter_emitted(once=once):
            emitted.append(msg)
            sys.stdout.write(to_jsonl(msg) + "\n")
            sys.stdout.flush()
        return emitted


def run_to_exit_code(
    run_loop: Callable[[], object],
    timeout_exc: type[Exception],
) -> int:
    """Run ``run_loop``, mapping ``timeout_exc`` to a non-zero exit code.

    Single boundary that converts an idle-timeout exception into the
    fail-fast-with-diagnostic-on-stderr exit code shared by ``cgc listen`` and
    ``cgc serve``: returns 0 on clean completion, 1 on idle-timeout expiry (the
    diagnostic is the exception's message). Keeps the per-command ``run()``
    wrappers from duplicating the same try/except/stderr/return-1 shape (DRY).
    """
    try:
        run_loop()
    except timeout_exc as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0
