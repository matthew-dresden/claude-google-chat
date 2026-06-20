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
from claude_google_chat.sessionrouter import SessionRouter
from claude_google_chat.state import FileStateStore, StateStore


class ListenerTimeout(RuntimeError):
    """Raised when the listener exceeds its configured idle timeout."""


def _timeout_message(timeout: float) -> str:
    return (
        f"no new Google Chat messages within {timeout}s idle timeout (CGC_LISTEN_TIMEOUT); exiting"
    )


def text_to_message(text: str, config: Config, *, is_human: bool) -> ChatMessage | None:
    """Convert raw message text to a :class:`ChatMessage`, applying the trigger rule.

    The single source of truth for the require-trigger-vs-catch-all conversion
    shared by the plain :class:`Listener` and the session-routing handler (DRY):

    - With ``require_trigger`` (the default) only a trigger-prefixed line is
      surfaced, parsed as a structured command; a non-prefixed line yields
      ``None`` (skip). The sender type is not consulted in this mode (a bot can
      only post a trigger line by re-emitting one, which dedup/own-post handling
      elsewhere already excludes).
    - With ``require_trigger`` cleared (catch-all) only a HUMAN line is surfaced
      (``is_human`` gates loop-prevention); a trigger-prefixed human line still
      parses as a command, a plain human line via
      :func:`message_from_human_text`.

    The returned message carries no ``thread_name`` — the caller attaches the
    owning thread.
    """
    prefix = config.trigger_prefix
    if config.require_trigger:
        if not text.strip().startswith(prefix):
            return None
        return parse_message(text, trigger_prefix=prefix)
    if not is_human:
        return None
    return message_from_human_text(text, trigger_prefix=prefix)


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
        router: SessionRouter | None = None,
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
            router: Optional :class:`~claude_google_chat.sessionrouter.SessionRouter`.
                When given, ``cgc listen --session NAME`` routing replaces the
                plain thread filter: only messages routed to the listening
                session are emitted (a reply in one of its claimed threads, or a
                ``NAME:`` message that claims a new thread), the dispatcher posts
                the "which session?" menu for truly-unrouted new threads, and each
                emitted event carries the session name + thread_name. The
                require-trigger / catch-all conversion and the resilient poll loop
                are reused unchanged.
        """
        self._config = config
        self._router = router
        resolved_fetcher = fetcher or (lambda cfg, since: list_messages(cfg, since=since))
        handler = router.handle if router is not None else self._handle
        self._loop = PollLoop(
            config,
            fetcher=lambda since: resolved_fetcher(config, since),
            handler=handler,
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

        text = raw.get("text", "")
        emitted = text_to_message(text, self._config, is_human=is_human_message(raw))
        if emitted is None:
            return None
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


def _session_router(config: Config, session: str) -> SessionRouter:
    """Build the production :class:`SessionRouter` for ``cgc listen --session``.

    Wires the file-backed session registry, the shared text→message conversion
    (DRY), and a webhook-backed menu sender. The listening session must already
    be registered (``cgc connect``); a missing session fails fast.
    """
    from pathlib import Path

    from claude_google_chat.chat import send_webhook
    from claude_google_chat.messages import ChatMessage as _ChatMessage
    from claude_google_chat.sessions import FileSessionRegistry, validate_session_name

    assert config.sessions_file is not None  # always resolved by Config.load
    registry = FileSessionRegistry(Path(config.sessions_file))
    sessions = registry.load()
    name = validate_session_name(session)
    if name not in sessions:
        raise ValueError(
            f"unknown session {name!r}; run 'cgc connect {name}' first (see 'cgc session list')"
        )

    def menu_sender(text: str, thread_key: str) -> str:
        # Post the menu into the unrouted thread. The thread already exists, so
        # the returned thread.name is not needed; an empty string is returned to
        # satisfy the (text, key) -> name contract without re-fetching.
        created = send_webhook(
            config,
            _ChatMessage(kind="status", status="info", text=text),
            thread_key=thread_key,
        )
        return created or ""

    def to_message(text: str, cfg: Config, is_human: bool) -> _ChatMessage | None:
        return text_to_message(text, cfg, is_human=is_human)

    return SessionRouter(
        config,
        session_name=name,
        registry=registry,
        to_message=to_message,
        menu_sender=menu_sender,
    )


def run(config: Config, *, once: bool = False, session: str | None = None) -> int:
    """Run the listener, emitting one JSON line per message to stdout.

    Returns a process exit code: 0 on clean completion (``--once`` drain), and
    non-zero on idle timeout or consecutive-error exhaustion (fail fast with a
    clear diagnostic on stderr). The high-water cursor is persisted to
    ``state_file`` so a restart resumes instead of re-emitting recent history.

    When ``session`` is given, routing-aware listening is enabled for that
    session: only messages routed to it are emitted (reply in a claimed thread,
    or a ``NAME:`` message that claims a new thread), the dispatcher answers
    truly-unrouted new threads with the "which session?" menu, and each emitted
    event carries the session name. The session must already be registered via
    ``cgc connect`` (fails fast otherwise).
    """
    router = _session_router(config, session) if session is not None else None
    listener = Listener(config, state_store=_state_store(config), router=router)
    return run_to_exit_code(lambda: listener.run_to_stdout(once=once), ListenerTimeout)
