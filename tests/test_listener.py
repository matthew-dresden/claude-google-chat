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
    """Build a listener Config; values are input-driven via overrides.

    ``state_file`` resolves to a path under ``/tmp`` only when a test drives the
    real ``run`` entrypoint (which builds a file-backed store); pass an explicit
    ``state_file`` (e.g. under ``tmp_path``) for those tests so no real config
    directory is touched.
    """
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
    tmp_path: Any,
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

    exit_code = run(_config(state_file=str(tmp_path / "state.json")), once=True)

    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    import json

    record = json.loads(out_lines[0])
    assert record["kind"] == "command"
    assert record["command"] == "deploy"


# --------------------------------------------------------------------------- #
# Catch-all mode (require_trigger=False): surface every HUMAN message, exclude
# bots/own posts. require_trigger=True path stays unchanged.
# --------------------------------------------------------------------------- #


def test_catch_all_surfaces_non_prefixed_human_message(
    frozen_clock: str,
    human_plain_message: dict[str, Any],
) -> None:
    """With require_trigger=False a plain HUMAN line is surfaced (not skipped)."""
    fetch, _ = _scripted_fetcher([[human_plain_message]])
    listener = Listener(_config(require_trigger=False), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    only = emitted[0]
    assert isinstance(only, ChatMessage)
    # The full message text is carried on the surfaced message.
    assert only.text == "just chatting, no command here"
    assert only.command == "just"


def test_catch_all_still_parses_trigger_prefixed_human_message_as_command(
    frozen_clock: str,
    human_trigger_message: dict[str, Any],
) -> None:
    """A trigger-prefixed HUMAN line still parses as a structured command."""
    fetch, _ = _scripted_fetcher([[human_trigger_message]])
    listener = Listener(_config(require_trigger=False), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    assert emitted[0].command == "deploy"
    assert emitted[0].args == ["prod"]


def test_catch_all_excludes_bot_and_own_messages(
    frozen_clock: str,
    bot_trigger_message: dict[str, Any],
) -> None:
    """A BOT/app message is never surfaced even in catch-all mode (loop guard)."""
    fetch, _ = _scripted_fetcher([[bot_trigger_message]])
    listener = Listener(_config(require_trigger=False), fetcher=fetch)

    assert list(listener.iter_new_messages(once=True)) == []


def test_catch_all_mixed_batch_emits_only_human_messages(
    frozen_clock: str,
    human_plain_message: dict[str, Any],
    human_trigger_message: dict[str, Any],
    bot_trigger_message: dict[str, Any],
) -> None:
    """A mixed batch surfaces both human messages and drops the bot message."""
    fetch, _ = _scripted_fetcher(
        [[human_plain_message, human_trigger_message, bot_trigger_message]]
    )
    listener = Listener(_config(require_trigger=False), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 2
    commands = {m.command for m in emitted}
    assert commands == {"just", "deploy"}


def test_require_trigger_true_path_unchanged(
    frozen_clock: str,
    human_plain_message: dict[str, Any],
    human_trigger_message: dict[str, Any],
) -> None:
    """The default (require_trigger=True) still emits only trigger-prefixed lines."""
    fetch, _ = _scripted_fetcher([[human_plain_message, human_trigger_message]])
    listener = Listener(_config(require_trigger=True), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    assert emitted[0].command == "deploy"


# --------------------------------------------------------------------------- #
# Thread routing: --thread / threads filter + thread_name on emitted events.
# These compose with require_trigger and the sender-type filter.
# --------------------------------------------------------------------------- #


def test_thread_name_is_carried_on_emitted_message(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """The owning raw ``thread.name`` is surfaced on each emitted ChatMessage."""
    raw = make_raw_message(
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        thread="spaces/AAAA/threads/T-emit",
    )
    fetch, _ = _scripted_fetcher([[raw]])
    listener = Listener(_config(), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    assert emitted[0].thread_name == "spaces/AAAA/threads/T-emit"


def test_thread_name_is_none_when_message_unthreaded(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """An unthreaded message surfaces ``thread_name=None`` (no filter configured)."""
    raw = make_raw_message(text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod", thread=None)
    fetch, _ = _scripted_fetcher([[raw]])
    listener = Listener(_config(), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert len(emitted) == 1
    assert emitted[0].thread_name is None


def test_thread_filter_emits_only_in_thread_messages(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """With a thread filter, only messages in a configured thread are emitted."""
    in_thread = make_raw_message(
        name="spaces/AAAA/messages/in",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        thread="spaces/AAAA/threads/keep",
    )
    out_thread = make_raw_message(
        name="spaces/AAAA/messages/out",
        text=f"{DEFAULT_TRIGGER_PREFIX} rollback staging",
        thread="spaces/AAAA/threads/other",
    )
    fetch, _ = _scripted_fetcher([[in_thread, out_thread]])
    listener = Listener(
        _config(threads=("spaces/AAAA/threads/keep",)),
        fetcher=fetch,
    )

    emitted = list(listener.iter_new_messages(once=True))

    assert [m.command for m in emitted] == ["deploy"]
    assert emitted[0].thread_name == "spaces/AAAA/threads/keep"


def test_thread_filter_drops_unthreaded_messages(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """A message with no thread is dropped when a thread filter is configured."""
    unthreaded = make_raw_message(text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod", thread=None)
    fetch, _ = _scripted_fetcher([[unthreaded]])
    listener = Listener(_config(threads=("spaces/AAAA/threads/keep",)), fetcher=fetch)

    assert list(listener.iter_new_messages(once=True)) == []


def test_thread_filter_accepts_multiple_threads(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """Multiple configured threads each admit their messages."""
    a = make_raw_message(
        name="spaces/AAAA/messages/a",
        text=f"{DEFAULT_TRIGGER_PREFIX} a",
        thread="spaces/AAAA/threads/T1",
    )
    b = make_raw_message(
        name="spaces/AAAA/messages/b",
        text=f"{DEFAULT_TRIGGER_PREFIX} b",
        thread="spaces/AAAA/threads/T2",
    )
    c = make_raw_message(
        name="spaces/AAAA/messages/c",
        text=f"{DEFAULT_TRIGGER_PREFIX} c",
        thread="spaces/AAAA/threads/T3",
    )
    fetch, _ = _scripted_fetcher([[a, b, c]])
    listener = Listener(
        _config(threads=("spaces/AAAA/threads/T1", "spaces/AAAA/threads/T2")),
        fetcher=fetch,
    )

    emitted = list(listener.iter_new_messages(once=True))

    assert {m.command for m in emitted} == {"a", "b"}


def test_thread_filter_composes_with_require_trigger(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """In-thread but non-trigger lines are still dropped under require_trigger."""
    trigger = make_raw_message(
        name="spaces/AAAA/messages/t",
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        thread="spaces/AAAA/threads/keep",
    )
    plain = make_raw_message(
        name="spaces/AAAA/messages/p",
        text="just chatting, no command",
        thread="spaces/AAAA/threads/keep",
    )
    fetch, _ = _scripted_fetcher([[trigger, plain]])
    listener = Listener(
        _config(threads=("spaces/AAAA/threads/keep",), require_trigger=True),
        fetcher=fetch,
    )

    emitted = list(listener.iter_new_messages(once=True))

    # Both are in-thread, but only the trigger-prefixed line passes require_trigger.
    assert [m.command for m in emitted] == ["deploy"]


def test_thread_filter_composes_with_sender_filter_in_catch_all(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """In catch-all mode the thread filter and HUMAN-only filter compose."""
    human = make_raw_message(
        name="spaces/AAAA/messages/h",
        text="hello team",
        sender_type="HUMAN",
        thread="spaces/AAAA/threads/keep",
    )
    bot = make_raw_message(
        name="spaces/AAAA/messages/b",
        text="bot says hi",
        sender_type="BOT",
        email=None,
        thread="spaces/AAAA/threads/keep",
    )
    fetch, _ = _scripted_fetcher([[human, bot]])
    listener = Listener(
        _config(threads=("spaces/AAAA/threads/keep",), require_trigger=False),
        fetcher=fetch,
    )

    emitted = list(listener.iter_new_messages(once=True))

    # The bot message is in-thread but excluded as a non-human sender.
    assert len(emitted) == 1
    assert emitted[0].text == "hello team"
    assert emitted[0].thread_name == "spaces/AAAA/threads/keep"


def test_no_thread_filter_surfaces_whole_space(
    frozen_clock: str,
    make_raw_message: Any,
) -> None:
    """With no threads configured, messages in any thread are surfaced (unchanged)."""
    a = make_raw_message(
        name="spaces/AAAA/messages/a",
        text=f"{DEFAULT_TRIGGER_PREFIX} a",
        thread="spaces/AAAA/threads/T1",
    )
    b = make_raw_message(
        name="spaces/AAAA/messages/b",
        text=f"{DEFAULT_TRIGGER_PREFIX} b",
        thread="spaces/AAAA/threads/T2",
    )
    fetch, _ = _scripted_fetcher([[a, b]])
    listener = Listener(_config(), fetcher=fetch)

    emitted = list(listener.iter_new_messages(once=True))

    assert {m.command for m in emitted} == {"a", "b"}


def test_thread_name_appears_in_emitted_json_line(
    frozen_clock: str,
    monkeypatch: pytest.MonkeyPatch,
    make_raw_message: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """The emitted stdout JSON line includes the owning ``thread_name``."""
    import json

    raw = make_raw_message(
        text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
        thread="spaces/AAAA/threads/T-json",
    )
    monkeypatch.setattr(
        "claude_google_chat.listener.list_messages",
        lambda config, since=None: [raw],
    )

    exit_code = run(
        _config(
            threads=("spaces/AAAA/threads/T-json",),
            state_file=str(tmp_path / "state.json"),
        ),
        once=True,
    )

    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    record = json.loads(out_lines[0])
    assert record["thread_name"] == "spaces/AAAA/threads/T-json"
    assert record["command"] == "deploy"


def test_run_returns_nonzero_on_idle_timeout(
    frozen_clock: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
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

    exit_code = run(
        _config(
            listen_timeout=10.0,
            poll_interval=1.0,
            state_file=str(tmp_path / "state.json"),
        ),
        once=False,
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "CGC_LISTEN_TIMEOUT" in err
