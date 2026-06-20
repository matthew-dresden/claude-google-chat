"""Tests for the poll-driven inbound :class:`Listener` loop.

These drive a bounded number of controlled poll iterations against an injected
fetcher (no network, no real ``chat.list_messages`` call). They assert:

- only trigger-prefixed messages are yielded;
- each message is emitted exactly once (dedup via the last-seen ``name`` set);
- the ``since`` high-water mark advances so subsequent polls filter server-side;
- the cadence ``sleeper`` paces continuous polling (it is not a readiness wait);
- an idle ``listen_timeout`` fails fast with a clear, actionable diagnostic.

The frozen clock keeps parsed timestamps deterministic; a monotonic test clock
and a stub sleeper make loop timing deterministic and free of real waits.
"""

from __future__ import annotations

from typing import Any

import pytest

from claude_google_chat.config import Config
from claude_google_chat.listener import Listener, ListenerTimeout, run
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, ChatMessage


def _config(**overrides: Any) -> Config:
    """Build a listener Config; values are input-driven via overrides."""
    base: dict[str, Any] = {
        "space_id": "spaces/AAAA",
        "trigger_prefix": DEFAULT_TRIGGER_PREFIX,
    }
    base.update(overrides)
    return Config(**base)


def _scripted_fetcher(
    pages: list[list[dict[str, Any]]],
) -> tuple[Any, list[str | None]]:
    """Return a fetcher that yields one queued page per call, and a since-log.

    The fetcher records the ``since`` argument it was called with so tests can
    assert the high-water mark advances across iterations. After the scripted
    pages are exhausted it returns an empty page (an idle poll).
    """
    calls: list[str | None] = []
    cursor = {"i": 0}

    def fetch(config: Config, since: str | None) -> list[dict[str, Any]]:
        calls.append(since)
        idx = cursor["i"]
        cursor["i"] = idx + 1
        if idx < len(pages):
            return pages[idx]
        return []

    return fetch, calls


def test_once_drains_only_trigger_messages(
    frozen_clock: str,
    make_raw_message: Any,
    human_trigger_message: dict[str, Any],
    human_plain_message: dict[str, Any],
) -> None:
    """A single ``--once`` drain yields only the trigger-prefixed message."""
    fetch, _ = _scripted_fetcher([[human_trigger_message, human_plain_message]])
    listener = Listener(_config(), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    only = emitted[0]
    assert isinstance(only, ChatMessage)
    assert only.kind == "command"
    assert only.command == "deploy"
    assert only.args == ["prod"]


def test_dedup_across_polls_emits_each_message_once(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """A message re-returned on a later poll is not yielded twice (last-seen)."""
    first = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        create_time="2026-06-20T12:00:00Z",
    )
    second = make_raw_message(
        name="spaces/AAAA/messages/2",
        text=f"{DEFAULT_TRIGGER_PREFIX} rollback staging",
        create_time="2026-06-20T12:00:01Z",
    )
    # Page 2 repeats message/1 (server overlap) and adds message/2.
    fetch, since_log = _scripted_fetcher([[first], [first, second]])
    # initial last_emit + (emit-reset + timeout-check) per batched poll, then a
    # final idle poll whose timeout check jumps past the window.
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 100.0])
    listener = Listener(
        _config(listen_timeout=10.0, poll_interval=1.0),
        fetcher=fetch,
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: None,
    )

    emitted: list[ChatMessage] = []
    with pytest.raises(ListenerTimeout):
        for msg in listener.iter_new_messages(once=False):
            emitted.append(msg)

    commands = [m.command for m in emitted]
    assert commands == ["deploy", "rollback"]
    # The duplicate message/1 on the second page was filtered by the seen-set.
    assert commands.count("deploy") == 1


def test_since_high_water_mark_advances_per_poll(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """The ``since`` argument advances to the newest seen createTime each poll."""
    first = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} one",
        create_time="2026-06-20T12:00:00Z",
    )
    second = make_raw_message(
        name="spaces/AAAA/messages/2",
        text=f"{DEFAULT_TRIGGER_PREFIX} two",
        create_time="2026-06-20T12:05:00Z",
    )
    fetch, since_log = _scripted_fetcher([[first], [second]])
    # Clock is consumed as: initial last_emit, then per iteration an emit-reset
    # (only when a batch is yielded) plus a timeout check. Keep the first three
    # polls inside the idle window, then jump past it to stop the loop.
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 100.0])
    listener = Listener(
        _config(listen_timeout=10.0, poll_interval=1.0),
        fetcher=fetch,
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(ListenerTimeout):
        list(listener.iter_new_messages(once=False))

    # First poll has no lower bound; the second is filtered by message/1's time;
    # the third (idle) poll is bounded by the newest createTime seen so far.
    assert since_log[0] is None
    assert since_log[1] == "2026-06-20T12:00:00Z"
    assert since_log[2] == "2026-06-20T12:05:00Z"


def test_cadence_sleeper_invoked_between_continuous_polls(
    frozen_clock: str,
) -> None:
    """The poll interval sleeper paces continuous polling before timeout."""
    sleeps: list[float] = []
    clock_values = iter([0.0, 0.0, 0.0, 50.0, 50.0])
    listener = Listener(
        _config(listen_timeout=10.0, poll_interval=2.5),
        fetcher=lambda config, since: [],
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ListenerTimeout):
        list(listener.iter_new_messages(once=False))

    # At least one cadence sleep fired, and it used the configured interval.
    assert sleeps
    assert all(s == 2.5 for s in sleeps)


def test_idle_timeout_fails_fast_with_actionable_message(
    frozen_clock: str,
) -> None:
    """No new message within the idle window raises a clear timeout error."""
    clock_values = iter([0.0, 0.0, 11.0, 11.0])
    listener = Listener(
        _config(listen_timeout=10.0, poll_interval=1.0),
        fetcher=lambda config, since: [],
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(ListenerTimeout) as exc_info:
        list(listener.iter_new_messages(once=False))

    message = str(exc_info.value)
    assert "10" in message
    assert "CGC_LISTEN_TIMEOUT" in message


def test_activity_resets_idle_timeout(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """Emitting a message resets the idle clock so the loop keeps running."""
    msg = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
    )
    # Poll 1 (t=0) emits -> last_emit reset at t=5. Poll 2 idle at t=8 (<10 from
    # 5). Poll 3 idle at t=20 (>=10 from 5) -> timeout.
    fetch, _ = _scripted_fetcher([[msg]])
    clock_values = iter([0.0, 5.0, 8.0, 8.0, 20.0, 20.0])
    listener = Listener(
        _config(listen_timeout=10.0, poll_interval=1.0),
        fetcher=fetch,
        clock=lambda: next(clock_values),
        sleeper=lambda seconds: None,
    )

    emitted: list[ChatMessage] = []
    with pytest.raises(ListenerTimeout):
        for m in listener.iter_new_messages(once=False):
            emitted.append(m)

    assert [m.command for m in emitted] == ["deploy"]


def test_run_once_emits_one_json_line_per_message(
    frozen_clock: str,
    monkeypatch: pytest.MonkeyPatch,
    make_raw_message: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run(once=True)`` writes one JSON line per yielded message and exits 0."""
    msg = make_raw_message(
        name="spaces/AAAA/messages/1",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
    )
    # Patch the default transport so ``run`` (which constructs its own Listener)
    # never touches the network.
    monkeypatch.setattr(
        "claude_google_chat.listener.list_messages",
        lambda config, since=None: [msg],
    )

    exit_code = run(_config(), once=True)

    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    import json

    record = json.loads(out_lines[0])
    assert record["kind"] == "command"
    assert record["command"] == "deploy"


def test_run_returns_nonzero_on_idle_timeout(
    frozen_clock: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run`` surfaces the idle timeout as a non-zero exit and stderr message."""
    monkeypatch.setattr(
        "claude_google_chat.listener.list_messages",
        lambda config, since=None: [],
    )
    clock_values = iter([0.0, 0.0, 11.0, 11.0])
    monkeypatch.setattr(
        "claude_google_chat.listener.time.monotonic",
        lambda: next(clock_values),
    )
    monkeypatch.setattr(
        "claude_google_chat.listener.time.sleep",
        lambda seconds: None,
    )

    exit_code = run(_config(listen_timeout=10.0, poll_interval=1.0), once=False)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "CGC_LISTEN_TIMEOUT" in err
