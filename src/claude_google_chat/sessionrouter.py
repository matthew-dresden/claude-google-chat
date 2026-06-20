"""Routing-aware per-message handler for ``cgc listen --session NAME``.

This is the side-effecting bridge between the pure routing decision in
:func:`claude_google_chat.sessions.route_message` and the resilient poll loop:
the :class:`SessionRouter` is a drop-in ``MessageHandler`` for
:class:`claude_google_chat.polling.PollLoop` (used via the
:class:`~claude_google_chat.listener.Listener`), so it inherits the existing
dedup, high-water, idle-timeout, and resilience machinery unchanged (DRY).

Per inbound HUMAN message in the space (raw ``sender.type == HUMAN``), with its
thread.name ``T`` and text, the router:

- **reply in one of my threads** (``T`` claimed by the listening session) →
  EMIT it as work;
- **NAME: in a new/unclaimed thread** (text starts with ``<my-name>:``) → CLAIM
  ``T`` (persist ``{name: T}`` under my session) and EMIT, with the ``NAME:``
  prefix stripped from the surfaced text;
- **dispatcher + truly-unrouted new thread** (unclaimed ``T``, text not starting
  with ANY registered session name, and I am the dispatcher) → post the "which
  session?" menu to ``T`` (do **not** emit it as work);
- **thread claimed by a different session** → skip.

Registry claims are persisted through the injected
:class:`~claude_google_chat.sessions.SessionRegistry`; the menu is posted through
an injected thread sender. Both are injectable so the router unit-tests with no
network and no disk. Each emitted :class:`ChatMessage` carries the session name
(``command``-independent ``correlation_id``-style metadata is avoided; the name is
surfaced via the dedicated field below) and the owning ``thread_name``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage
from claude_google_chat.rawmessage import is_human_message, thread_name
from claude_google_chat.sessions import (
    ROUTE_CLAIM,
    ROUTE_EMIT,
    ROUTE_MENU,
    SessionRegistry,
    add_thread_to_session,
    dispatcher_menu_text,
    route_message,
)

# Posts ``text`` into the thread keyed by ``thread_key`` and returns the created
# Chat ``thread.name``. Reused from the connect path; here it posts the menu into
# the unrouted thread. Injectable so tests use a fake (no network).
MenuSender = Callable[[str, str], str]


class SessionRouter:
    """A routing-aware ``MessageHandler`` for one listening session.

    Holds the listening session's name, the durable registry, the message→event
    conversion (the same require-trigger / catch-all rule the plain listener
    uses, injected so it is not duplicated here), and the menu sender. Returns a
    :class:`ChatMessage` to emit, or ``None`` to skip — exactly the
    ``MessageHandler`` contract the poll loop expects.
    """

    def __init__(
        self,
        config: Config,
        *,
        session_name: str,
        registry: SessionRegistry,
        to_message: Callable[[str, Config, bool], ChatMessage | None],
        menu_sender: MenuSender | None = None,
    ) -> None:
        """Initialise the router.

        Args:
            config: Resolved configuration (carries the trigger prefix and
                require-trigger mode the conversion honours).
            session_name: The session this listener belongs to (the routing
                target). Must already be registered.
            registry: Durable session map; loaded each poll and saved on a claim
                so a claim survives a restart. Injectable for tests.
            to_message: The text→:class:`ChatMessage` conversion
                (``claude_google_chat.listener.text_to_message``), injected to
                avoid duplicating the require-trigger / catch-all logic (DRY).
            menu_sender: Posts the dispatcher "which session?" menu into an
                unrouted thread (``(text, thread_key) -> thread.name``).
                Injectable; required only when this session is the dispatcher.
        """
        self._config = config
        self._session_name = session_name
        self._registry = registry
        self._to_message = to_message
        self._menu_sender = menu_sender

    def _emit(self, text: str, raw_thread: str | None, is_human: bool) -> ChatMessage | None:
        """Convert routed text to an emittable event carrying session + thread."""
        message = self._to_message(text, self._config, is_human)
        # ``_to_message`` matches ``listener.text_to_message`` (positional bool).
        if message is None:
            return None
        return replace(
            message,
            thread_name=raw_thread,
            session_name=self._session_name,
        )

    def handle(self, raw: dict[str, Any]) -> ChatMessage | None:
        """Route one raw Chat message for the listening session.

        Non-human senders are never surfaced (loop prevention), mirroring the
        catch-all listener. For a human message the pure
        :func:`route_message` decision is applied and its side effect performed:
        EMIT surfaces the text; CLAIM persists the new thread under this session
        and surfaces the prefix-stripped text; MENU posts the menu via the
        injected sender and surfaces nothing; SKIP surfaces nothing.
        """
        if not is_human_message(raw):
            return None

        raw_thread = thread_name(raw)
        text = raw.get("text", "")
        sessions = self._registry.load()
        decision = route_message(
            sessions,
            listening_session=self._session_name,
            thread_name=raw_thread,
            text=text,
        )

        if decision.action == ROUTE_EMIT:
            return self._emit(decision.text, raw_thread, is_human=True)

        if decision.action == ROUTE_CLAIM:
            # Persist the claim before emitting so a crash after emit still has
            # the thread recorded for the next poll. The thread key is unknown
            # (the thread was created by the human, not by our outbound send).
            assert raw_thread is not None  # route_message only CLAIMs a threaded msg
            sessions = add_thread_to_session(
                sessions,
                name=self._session_name,
                thread_name=raw_thread,
            )
            self._registry.save(sessions)
            return self._emit(decision.text, raw_thread, is_human=True)

        if decision.action == ROUTE_MENU:
            if self._menu_sender is not None and raw_thread is not None:
                self._menu_sender(dispatcher_menu_text(sessions), raw_thread)
            return None

        # ROUTE_SKIP.
        return None
