"""Tests for the resilient + durable behavior of :class:`PollLoop`.

These drive the shared poll loop directly through injected callables (a scripted
fetcher, a test clock, a stub sleeper, an in-memory or file-backed state store,
and a capturing error stream) so no network, disk-in-unit-loop, or real sleep is
involved. They assert the production-grade guarantees:

- a transient fetch error is caught, a concise diagnostic is emitted, and the
  loop continues to the next poll;
- the consecutive-error counter resets after any successful poll;
- the configured ``max_consecutive_errors`` bound fails fast with a non-zero
  diagnostic once reached;
- a fatal ``401``/``403`` auth error fails fast immediately (no retry);
- the high-water marker is persisted to the injected store and a restart resumes
  from it (no re-emit of already-seen messages).
"""

from __future__ import annotations

from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from claude_google_chat.config import Config
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, ChatMessage
from claude_google_chat.polling import PollLoop, PollLoopExhausted
from claude_google_chat.state import FileStateStore, InMemoryStateStore


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "space_id": "spaces/AAAA",
        "trigger_prefix": DEFAULT_TRIGGER_PREFIX,
    }
    base.update(overrides)
    return Config(**base)


def _http_error(status: int) -> HttpError:
    resp = Response({"status": status})
    return HttpError(resp, b'{"error": {"message": "x"}}', uri="https://chat.googleapis.com")


def _passthrough_handler(raw: dict[str, Any]) -> ChatMessage | None:
    """Emit a trivial command message for any raw record (transport-agnostic)."""
    return ChatMessage(kind="command", command="ok", text=raw.get("text", ""))


def _timeout_message(timeout: float) -> str:
    return f"idle {timeout}s (CGC_LISTEN_TIMEOUT)"


class _IdleTimeout(RuntimeError):
    """Distinct idle-timeout exception type for these loop tests."""


def _loop(
    fetcher: Any,
    *,
    config: Config | None = None,
    clock: Any,
    sleeper: Any = lambda seconds: None,
    state_store: Any = None,
    error_stream: Any = None,
) -> PollLoop:
    return PollLoop(
        config or _config(),
        fetcher=fetcher,
        handler=_passthrough_handler,
        timeout_exc=_IdleTimeout,
        timeout_message=_timeout_message,
        clock=clock,
        sleeper=sleeper,
        state_store=state_store,
        error_stream=error_stream,
    )


# --------------------------------------------------------------------------- #
# Transient error -> caught, logged, loop continues.
# --------------------------------------------------------------------------- #


def test_transient_error_is_caught_and_loop_continues() -> None:
    """A transient fetch error is absorbed; a later good poll still emits."""
    calls = {"i": 0}

    def fetch(since: str | None) -> list[dict[str, Any]]:
        idx = calls["i"]
        calls["i"] = idx + 1
        if idx == 0:
            raise TimeoutError("timed out")
        if idx == 1:
            return [{"name": "spaces/AAAA/messages/1", "createTime": "2026-06-20T12:00:00Z"}]
        return []

    diagnostics: list[str] = []
    # Clock: initial last_activity, then per iteration (timeout-check + emit-reset)
    # kept inside the window until a final jump past it stops the loop.
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 100.0])
    loop = _loop(
        fetch,
        config=_config(listen_timeout=10.0, poll_interval=1.0),
        clock=lambda: next(clock_values),
        error_stream=diagnostics.append,
    )

    emitted: list[ChatMessage] = []
    with pytest.raises(_IdleTimeout):
        for msg in loop.iter_emitted():
            emitted.append(msg)

    # The good poll after the transient error still produced its message.
    assert len(emitted) == 1
    # A concise diagnostic for the transient error was written exactly once.
    assert len(diagnostics) == 1
    assert "transient" in diagnostics[0].lower()


def test_consecutive_error_counter_resets_after_success() -> None:
    """A success between transient errors prevents premature exhaustion."""
    # Pattern: fail, success, fail, fail (limit=3 must NOT trip — never 3 in a row).
    script: list[Any] = [
        TimeoutError("t1"),
        [],  # success resets the counter
        TimeoutError("t2"),
        TimeoutError("t3"),
        [],  # success resets again
    ]
    calls = {"i": 0}

    def fetch(since: str | None) -> list[dict[str, Any]]:
        idx = calls["i"]
        calls["i"] = idx + 1
        item = script[idx] if idx < len(script) else []
        if isinstance(item, BaseException):
            raise item
        return item

    diagnostics: list[str] = []
    clock_values = iter([0.0] + [0.0] * 12 + [100.0, 100.0])
    loop = _loop(
        fetch,
        config=_config(listen_timeout=10.0, poll_interval=1.0, max_consecutive_errors=3),
        clock=lambda: next(clock_values),
        error_stream=diagnostics.append,
    )

    with pytest.raises(_IdleTimeout):
        list(loop.iter_emitted())

    # Three transient errors total, but never three consecutively -> no exhaustion.
    assert len(diagnostics) == 3


# --------------------------------------------------------------------------- #
# Consecutive-error bound -> fail fast.
# --------------------------------------------------------------------------- #


def test_consecutive_error_bound_fails_fast_at_limit() -> None:
    """After ``max_consecutive_errors`` transient failures the loop fails fast."""

    def fetch(since: str | None) -> list[dict[str, Any]]:
        raise TimeoutError("timed out")

    diagnostics: list[str] = []
    loop = _loop(
        fetch,
        config=_config(max_consecutive_errors=4, poll_interval=1.0),
        clock=lambda: 0.0,
        error_stream=diagnostics.append,
    )

    with pytest.raises(PollLoopExhausted) as exc_info:
        list(loop.iter_emitted())

    message = str(exc_info.value)
    assert "4" in message
    assert "CGC_MAX_CONSECUTIVE_ERRORS" in message
    # One diagnostic per failed attempt up to and including the limit.
    assert len(diagnostics) == 4


def test_http_429_counts_toward_consecutive_bound() -> None:
    """A Chat API 429 is transient and counts toward the exhaustion bound."""

    def fetch(since: str | None) -> list[dict[str, Any]]:
        raise _http_error(429)

    loop = _loop(
        fetch,
        config=_config(max_consecutive_errors=2, poll_interval=1.0),
        clock=lambda: 0.0,
        error_stream=lambda line: None,
    )
    with pytest.raises(PollLoopExhausted):
        list(loop.iter_emitted())


# --------------------------------------------------------------------------- #
# Fatal auth error -> fail fast immediately.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403])
def test_fatal_auth_error_fails_fast_immediately(status: int) -> None:
    """A 401/403 propagates on the first poll without being retried."""
    attempts = {"n": 0}

    def fetch(since: str | None) -> list[dict[str, Any]]:
        attempts["n"] += 1
        raise _http_error(status)

    loop = _loop(
        fetch,
        config=_config(max_consecutive_errors=10, poll_interval=1.0),
        clock=lambda: 0.0,
        error_stream=lambda line: None,
    )

    with pytest.raises(HttpError):
        list(loop.iter_emitted())
    # The fatal error was not retried: exactly one fetch attempt was made.
    assert attempts["n"] == 1


def test_non_transient_error_propagates_without_retry() -> None:
    """A ValueError (neither transient nor fatal-auth) is not absorbed."""

    def fetch(since: str | None) -> list[dict[str, Any]]:
        raise ValueError("programming error")

    loop = _loop(fetch, clock=lambda: 0.0, error_stream=lambda line: None)
    with pytest.raises(ValueError):
        list(loop.iter_emitted())


# --------------------------------------------------------------------------- #
# Durable high-water -> restart resumes, no re-emit.
# --------------------------------------------------------------------------- #


def test_high_water_marker_is_persisted_on_emit() -> None:
    store = InMemoryStateStore()
    msg = {"name": "spaces/AAAA/messages/1", "createTime": "2026-06-20T12:00:00Z"}
    loop = _loop(lambda since: [msg], clock=lambda: 0.0, state_store=store)
    loop.poll_once()
    assert store.load() == "2026-06-20T12:00:00Z"


def test_restart_resumes_from_persisted_marker_without_re_emit(tmp_path: Any) -> None:
    """A second loop instance over a shared file resumes and re-emits nothing."""
    state_path = tmp_path / "listen-state.json"
    older = {"name": "spaces/AAAA/messages/1", "createTime": "2026-06-20T12:00:00Z"}
    newer = {"name": "spaces/AAAA/messages/2", "createTime": "2026-06-20T12:05:00Z"}

    # First run sees both messages and persists the newest createTime.
    seen_since_first: list[str | None] = []

    def first_fetch(since: str | None) -> list[dict[str, Any]]:
        seen_since_first.append(since)
        return [older, newer]

    first = _loop(first_fetch, clock=lambda: 0.0, state_store=FileStateStore(state_path))
    first_emitted = first.poll_once()
    assert len(first_emitted) == 2
    assert seen_since_first[0] is None  # cold start: no prior marker

    # Simulate a restart: a brand-new loop instance over the same state file.
    seen_since_second: list[str | None] = []

    def second_fetch(since: str | None) -> list[dict[str, Any]]:
        seen_since_second.append(since)
        # A correct server-side filter would return nothing newer than the marker;
        # even if the server re-returned the old messages, dedup must drop them.
        return []

    second = _loop(second_fetch, clock=lambda: 0.0, state_store=FileStateStore(state_path))
    second_emitted = second.poll_once()

    # The restart resumed from the persisted high-water marker (not None) and
    # emitted nothing already seen.
    assert second_emitted == []
    assert seen_since_second[0] == "2026-06-20T12:05:00Z"
    assert second.since == "2026-06-20T12:05:00Z"


def test_cold_start_with_empty_store_has_no_marker() -> None:
    loop = _loop(lambda since: [], clock=lambda: 0.0, state_store=InMemoryStateStore())
    assert loop.since is None


def test_default_error_stream_writes_diagnostic_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no injected error stream, the transient diagnostic lands on stderr."""

    def fetch(since: str | None) -> list[dict[str, Any]]:
        raise TimeoutError("timed out")

    loop = PollLoop(
        _config(max_consecutive_errors=1, poll_interval=1.0),
        fetcher=fetch,
        handler=_passthrough_handler,
        timeout_exc=_IdleTimeout,
        timeout_message=_timeout_message,
        clock=lambda: 0.0,
    )
    with pytest.raises(PollLoopExhausted):
        list(loop.iter_emitted())

    err = capsys.readouterr().err
    assert "transient" in err.lower()
