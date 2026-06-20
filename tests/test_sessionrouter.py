"""Tests for the routing-aware per-message handler (`SessionRouter`).

The registry and the menu sender are injected, so these run with no network and
no disk. They assert the side-effecting outcomes of each routing branch: a reply
in an own thread emits (carrying session + thread_name), a ``NAME:`` new thread
is claimed (persisted) and emitted with the prefix stripped, a thread claimed by
another session is skipped, the dispatcher posts the menu for a truly-unrouted
message (and does NOT emit it as work) but does NOT menu a named message, and a
non-human sender is always dropped (loop prevention).
"""

from __future__ import annotations

from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.listener import text_to_message
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX, ChatMessage
from claude_google_chat.sessionrouter import SessionRouter
from claude_google_chat.sessions import (
    InMemorySessionRegistry,
    Session,
    SessionThread,
)

SPACE = "spaces/AAAA"


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "space_id": SPACE,
        "trigger_prefix": DEFAULT_TRIGGER_PREFIX,
        "require_trigger": False,  # catch-all so plain text surfaces
    }
    base.update(overrides)
    return Config(**base)


class _MenuSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, thread_key: str) -> str:
        self.calls.append((text, thread_key))
        return thread_key


def _raw(
    *,
    text: str,
    thread: str | None,
    sender_type: str = "HUMAN",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": f"{SPACE}/messages/{abs(hash((text, thread))) % 10000}",
        "text": text,
        "createTime": "2026-06-20T12:00:00Z",
        "sender": {"type": sender_type},
    }
    if thread is not None:
        raw["thread"] = {"name": thread}
    return raw


def _registry_two() -> InMemorySessionRegistry:
    """alpha (dispatcher, claims T1) + beta (claims T2)."""
    return InMemorySessionRegistry(
        {
            "alpha": Session(
                name="alpha",
                space_id=SPACE,
                threads=(SessionThread(name=f"{SPACE}/threads/T1", key="alpha"),),
                dispatcher=True,
            ),
            "beta": Session(
                name="beta",
                space_id=SPACE,
                threads=(SessionThread(name=f"{SPACE}/threads/T2", key="beta"),),
            ),
        }
    )


def _router(
    registry: InMemorySessionRegistry,
    session_name: str,
    *,
    menu_sender: _MenuSender | None = None,
    config: Config | None = None,
) -> SessionRouter:
    return SessionRouter(
        config or _config(),
        session_name=session_name,
        registry=registry,
        to_message=lambda text, cfg, human: text_to_message(text, cfg, is_human=human),
        menu_sender=menu_sender,
    )


# --------------------------------------------------------------------------- #
# Routing branches.
# --------------------------------------------------------------------------- #


def test_reply_in_own_thread_emits_with_session_and_thread() -> None:
    registry = _registry_two()
    router = _router(registry, "alpha")
    result = router.handle(_raw(text="restart now", thread=f"{SPACE}/threads/T1"))
    assert isinstance(result, ChatMessage)
    assert result.text == "restart now"
    assert result.thread_name == f"{SPACE}/threads/T1"
    assert result.session_name == "alpha"


def test_named_new_thread_is_claimed_and_emitted_prefix_stripped() -> None:
    registry = _registry_two()
    router = _router(registry, "beta")
    result = router.handle(_raw(text="beta: run migration", thread=f"{SPACE}/threads/NEW"))
    assert isinstance(result, ChatMessage)
    # Prefix stripped from the surfaced text.
    assert result.text == "run migration"
    assert result.session_name == "beta"
    # The claim was persisted to the registry.
    claimed = registry.load()["beta"].thread_names()
    assert f"{SPACE}/threads/NEW" in claimed


def test_thread_claimed_by_other_session_is_skipped() -> None:
    registry = _registry_two()
    router = _router(registry, "alpha")
    # T2 belongs to beta.
    assert router.handle(_raw(text="hi", thread=f"{SPACE}/threads/T2")) is None


def test_dispatcher_posts_menu_for_unrouted_new_thread_and_does_not_emit() -> None:
    registry = _registry_two()
    menu = _MenuSender()
    router = _router(registry, "alpha", menu_sender=menu)
    result = router.handle(_raw(text="is anyone there", thread=f"{SPACE}/threads/NEW"))
    assert result is None  # not emitted as work
    assert len(menu.calls) == 1
    menu_text, thread_key = menu.calls[0]
    assert thread_key == f"{SPACE}/threads/NEW"
    assert "alpha" in menu_text and "beta" in menu_text


def test_dispatcher_does_not_menu_named_message() -> None:
    registry = _registry_two()
    menu = _MenuSender()
    router = _router(registry, "alpha", menu_sender=menu)
    # Addressed to beta -> alpha (dispatcher) must not menu and must not emit.
    result = router.handle(_raw(text="beta: do it", thread=f"{SPACE}/threads/NEW"))
    assert result is None
    assert menu.calls == []


def test_non_human_sender_is_always_dropped() -> None:
    registry = _registry_two()
    menu = _MenuSender()
    router = _router(registry, "alpha", menu_sender=menu)
    result = router.handle(_raw(text="alpha: hi", thread=f"{SPACE}/threads/T1", sender_type="BOT"))
    assert result is None
    assert menu.calls == []


def test_claim_persists_so_next_reply_in_thread_emits() -> None:
    registry = _registry_two()
    router = _router(registry, "beta")
    # First message claims the new thread.
    router.handle(_raw(text="beta: start", thread=f"{SPACE}/threads/NEW"))
    # A follow-up reply in the now-claimed thread (no NAME: prefix) emits.
    result = router.handle(_raw(text="continue", thread=f"{SPACE}/threads/NEW"))
    assert isinstance(result, ChatMessage)
    assert result.text == "continue"
    assert result.session_name == "beta"


def test_require_trigger_mode_skips_non_prefixed_in_own_thread() -> None:
    # With require_trigger=True a reply in an own thread that is NOT trigger
    # -prefixed is converted to None (skip), so nothing is emitted.
    registry = _registry_two()
    router = _router(registry, "alpha", config=_config(require_trigger=True))
    result = router.handle(_raw(text="plain text", thread=f"{SPACE}/threads/T1"))
    assert result is None


def test_require_trigger_mode_emits_prefixed_reply_in_own_thread() -> None:
    registry = _registry_two()
    router = _router(registry, "alpha", config=_config(require_trigger=True))
    result = router.handle(
        _raw(
            text=f"{DEFAULT_TRIGGER_PREFIX} deploy prod",
            thread=f"{SPACE}/threads/T1",
        )
    )
    assert isinstance(result, ChatMessage)
    assert result.command == "deploy"
    assert result.session_name == "alpha"
