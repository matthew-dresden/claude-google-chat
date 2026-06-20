"""Shared Google Chat polling primitive.

The inbound listener (``cgc listen``) polls the space on an env-driven cadence
and needs consistent bookkeeping: per-message ``name`` dedup, ``createTime``
high-water tracking, an idle-timeout-with-monotonic-clock run loop, and
one-JSON-line-per-message stdout emission (12-factor logs). This module holds
that single implementation so any caller plugs in only a per-message predicate
and action (DRY).

The poll cadence is a documented, env-driven cadence (``poll_interval``), not a
readiness ``sleep``; the idle ``listen_timeout`` (when > 0) fails fast with a
clear, caller-supplied diagnostic.

The loop is **resilient**: a transient fetch/handle error (a socket timeout, a
dropped connection, or a Chat API ``408``/``429``/``5xx``) is caught, logged as a
concise secret-free diagnostic to stderr, and the loop continues on the normal
cadence rather than crashing. A **fatal** auth/permission error (``401``/``403``)
still fails fast. A configurable bound (``max_consecutive_errors``) fails the
loop fast once that many consecutive transient failures occur, so a truly-down
backend still surfaces a non-zero exit. The high-water cursor is **durable** via
an injected :class:`~claude_google_chat.state.StateStore`, so a restart resumes
from the last processed message instead of re-emitting recent history.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, to_jsonl
from claude_google_chat.resilience import diagnostic, is_fatal_error, is_transient_error
from claude_google_chat.state import InMemoryStateStore, StateStore

# Maps a raw Chat message resource (already deduped + high-water-tracked) to an
# outbound :class:`ChatMessage` to emit, or ``None`` to skip it.
MessageHandler = Callable[[dict[str, Any]], "ChatMessage | None"]

# Returns the records newer than ``since`` (raw Chat message resources).
RawFetcher = Callable[[str | None], list[dict[str, Any]]]

# Builds the idle-timeout diagnostic message from the configured timeout value.
TimeoutMessage = Callable[[float], str]


class PollLoopExhausted(RuntimeError):
    """Raised when consecutive transient failures exceed the configured bound.

    Signals a truly-down backend (rather than a passing hiccup) so the loop
    fails fast with a non-zero exit instead of retrying forever.
    """


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
        state_store: StateStore | None = None,
        error_stream: Callable[[str], None] | None = None,
    ) -> None:
        """Initialise the poll loop.

        Args:
            config: Resolved configuration (cadence + idle timeout + the
                consecutive-error bound).
            fetcher: Returns raw Chat messages newer than ``since`` (injectable
                for tests so no network access is required).
            handler: Per-message predicate/action returning a :class:`ChatMessage`
                to emit, or ``None`` to skip.
            timeout_exc: Exception type raised on idle-timeout expiry (fail fast).
            timeout_message: Builds the diagnostic string for ``timeout_exc``.
            clock: Monotonic clock source (injectable for tests).
            sleeper: Cadence sleeper between polls (injectable; paces polling,
                not a readiness wait).
            state_store: Durable high-water store. Defaults to a non-durable
                in-memory store (injectable for tests; the CLI supplies a
                file-backed store so a restart resumes).
            error_stream: Sink for transient-error diagnostics. Defaults to
                ``stderr`` (injectable for tests so diagnostics are assertable
                without capturing the real stream).
        """
        self._config = config
        self._fetcher = fetcher
        self._handler = handler
        self._timeout_exc = timeout_exc
        self._timeout_message = timeout_message
        self._clock = clock
        self._sleeper = sleeper
        self._state_store: StateStore = (
            state_store if state_store is not None else InMemoryStateStore()
        )
        self._error_stream = error_stream if error_stream is not None else _stderr_write
        self._seen: set[str] = set()
        # Resume from the durable high-water marker so a restart never re-emits
        # already-seen messages.
        self._since: str | None = self._state_store.load()
        self._consecutive_errors = 0

    @property
    def since(self) -> str | None:
        """Highest ``createTime`` seen so far (the poll cursor)."""
        return self._since

    def _advance_high_water(self, create_time: str) -> None:
        """Advance and durably persist the high-water marker if ``create_time`` is newer."""
        if self._since is None or create_time > self._since:
            self._since = create_time
            self._state_store.save(create_time)

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
            if create_time:
                self._advance_high_water(create_time)
            result = self._handler(raw)
            if result is not None:
                emitted.append(result)
        return emitted

    def _poll_resiliently(self) -> list[ChatMessage]:
        """Run one poll, surviving transient errors and failing fast on fatal ones.

        Returns the batch emitted by :meth:`poll_once`. On a transient error the
        consecutive-error counter is incremented, a concise secret-free
        diagnostic is written to the error stream, and an empty batch is
        returned so the caller continues on the normal cadence. The counter is
        reset to zero on any successful poll. A fatal auth/permission error is
        re-raised immediately; exceeding ``max_consecutive_errors`` consecutive
        transient failures raises :class:`PollLoopExhausted` (fail fast).
        """
        try:
            batch = self.poll_once()
        except Exception as exc:
            # Classify: fatal auth/permission errors and anything not recognised
            # as transient re-raise immediately; only transient errors are
            # absorbed so the loop keeps polling.
            if is_fatal_error(exc) or not is_transient_error(exc):
                raise
            self._consecutive_errors += 1
            self._error_stream(diagnostic(exc))
            limit = self._config.max_consecutive_errors
            if self._consecutive_errors >= limit:
                raise PollLoopExhausted(
                    f"giving up after {self._consecutive_errors} consecutive transient poll "
                    f"errors (CGC_MAX_CONSECUTIVE_ERRORS={limit}); the Chat backend appears "
                    "unavailable"
                ) from exc
            return []
        self._consecutive_errors = 0
        return batch

    def iter_emitted(self, *, once: bool = False) -> Iterator[ChatMessage]:
        """Yield each emitted message, polling on cadence with an idle timeout.

        Args:
            once: If true, drain currently-pending messages and stop.

        Raises:
            The configured ``timeout_exc`` if ``listen_timeout`` is > 0 and no
            message is emitted within that idle window (fail fast).
            :class:`PollLoopExhausted` if consecutive transient errors exceed
            ``max_consecutive_errors`` (fail fast).
        """
        last_activity = self._clock()
        while True:
            batch = self._poll_resiliently()
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


def _stderr_write(line: str) -> None:
    """Write a single diagnostic line to stderr (unbuffered, 12-factor logs)."""
    sys.stderr.write(f"{line}\n")
    sys.stderr.flush()


def run_to_exit_code(
    run_loop: Callable[[], object],
    timeout_exc: type[Exception],
) -> int:
    """Run ``run_loop``, mapping idle-timeout / exhaustion to a non-zero exit code.

    Single boundary that converts a fail-fast loop exception into the
    fail-fast-with-diagnostic-on-stderr exit code used by ``cgc listen``:
    returns 0 on clean completion, 1 on idle-timeout expiry or on
    consecutive-error exhaustion (the diagnostic is the exception's message).
    Keeps the per-command ``run()`` wrappers from duplicating the same
    try/except/stderr/return-1 shape (DRY).
    """
    try:
        run_loop()
    except (timeout_exc, PollLoopExhausted) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0
