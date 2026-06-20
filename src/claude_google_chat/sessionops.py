"""Orchestration for the session layer (connect / list / disconnect).

This module wires the pure session primitives in
:mod:`claude_google_chat.sessions` to their side effects — the durable registry,
the threaded Chat send, and the wall clock — behind **injectable** seams so the
orchestration is testable with no network, no disk, and no real clock:

- ``registry``: a :class:`~claude_google_chat.sessions.SessionRegistry` (file or
  in-memory) loaded/saved here.
- ``sender``: a callable ``(message_text, thread_key) -> thread_name`` that posts
  the opening message into a caller-keyed thread and returns the created
  ``thread.name``. In production this is backed by
  :func:`claude_google_chat.chat.send_webhook`; tests inject a fake.
- ``clock``: an RFC3339 timestamp source.

The CLI (:mod:`claude_google_chat.cli`) supplies the production seams; this layer
holds the order-of-operations and message text so it can be unit-tested directly.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from claude_google_chat.config import Config
from claude_google_chat.sessions import (
    Session,
    SessionRegistry,
    add_thread_to_session,
    derive_session_name,
    now_rfc3339,
    remove_session,
    routing_instructions,
    upsert_session,
    validate_session_name,
)
from claude_google_chat.validation import validate_space_id

# Posts ``text`` into the thread keyed by ``thread_key`` and returns the created
# Chat ``thread.name``. Mirrors the (text, thread_key) → thread.name contract of
# the threaded webhook send. Injectable so tests use a fake (no network).
ThreadSender = Callable[[str, str], str]


def _git_output(args: list[str], cwd: str) -> str | None:
    """Run a read-only ``git`` command in ``cwd``, returning trimmed stdout or ``None``.

    Returns ``None`` (rather than raising) when git is absent, the directory is
    not a repo, or the command fails — the caller degrades to a default name
    component. No shell is used (argument list), so there is no injection surface.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return out or None


def git_repo_name(cwd: str) -> str | None:
    """Return the basename of the git toplevel for ``cwd``, or ``None`` if not a repo."""
    toplevel = _git_output(["rev-parse", "--show-toplevel"], cwd)
    if toplevel is None:
        return None
    return Path(toplevel).name or None


def git_branch_name(cwd: str) -> str | None:
    """Return the current git branch for ``cwd``, or ``None`` if unavailable/detached."""
    return _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def resolve_session_name(
    explicit: str | None,
    *,
    cwd: str,
    repo_name: Callable[[str], str | None] = git_repo_name,
    branch_name: Callable[[str], str | None] = git_branch_name,
) -> str:
    """Resolve the session name: the explicit arg, else derive one deterministically.

    An explicit ``NAME`` is validated and returned. Otherwise a stable name is
    derived from the git repo + branch + a short hash of ``cwd`` (see
    :func:`claude_google_chat.sessions.derive_session_name`). ``repo_name`` and
    ``branch_name`` are injectable so the derivation is tested without a real git
    repo. Deterministic: same inputs → same name (so ``connect`` is idempotent).
    """
    if explicit is not None and explicit.strip():
        return validate_session_name(explicit.strip())
    return derive_session_name(
        repo=repo_name(cwd),
        branch=branch_name(cwd),
        cwd=cwd,
    )


def _opening_message_text(name: str) -> str:
    """Build the opening message posted to a session's primary thread on connect."""
    return (
        f"Session '{name}' connected. Reply in this thread to talk to it, or start "
        f"a new thread with '{name}: <message>'."
    )


def _disconnect_message_text(name: str) -> str:
    """Build the note posted to a session's primary thread on disconnect."""
    return f"Session '{name}' disconnected. This thread is no longer routed."


def connect_session(
    config: Config,
    *,
    name: str | None,
    space_id: str | None,
    dispatcher: bool,
    registry: SessionRegistry,
    sender: ThreadSender,
    cwd: str,
    clock: Callable[[], str] = now_rfc3339,
    repo_name: Callable[[str], str | None] = git_repo_name,
    branch_name: Callable[[str], str | None] = git_branch_name,
) -> Session:
    """Create or reuse a session and its primary thread, persisting the registry.

    Steps (idempotent on the resolved ``name``):

    1. Resolve ``name`` (explicit or derived from git + cwd).
    2. Resolve the shared space from ``space_id`` or ``config.space_id`` (fails
       fast, validated form).
    3. Upsert the session record (auto-marking the first session as dispatcher,
       or honouring ``--dispatcher``).
    4. If the session has **no** primary thread yet, send the opening message via
       ``sender`` (thread key = the session name) and record the returned
       ``thread.name`` as the primary thread. Reconnecting an existing session
       that already has a primary thread does **not** post again or duplicate the
       thread.
    5. Persist the registry and return the resulting session.

    Pure side effects are confined to ``registry`` and ``sender`` (both injected),
    so this is unit-tested with no network or disk.
    """
    resolved_name = resolve_session_name(
        name, cwd=cwd, repo_name=repo_name, branch_name=branch_name
    )
    resolved_space = space_id if space_id is not None else config.space_id
    if not resolved_space:
        raise ValueError("no space configured; set space_id (CGC_SPACE_ID) or pass --space")
    space = validate_space_id(resolved_space)

    sessions = registry.load()
    sessions = upsert_session(
        sessions,
        name=resolved_name,
        space_id=space,
        dispatcher=dispatcher,
        clock=clock,
    )

    session = sessions[resolved_name]
    if session.primary_thread is None:
        # The thread key is the session name so a re-send with the same key lands
        # in the same thread (idempotent at the Chat layer too).
        thread_name = sender(_opening_message_text(resolved_name), resolved_name)
        if not thread_name:
            raise ValueError(
                f"opening send for session {resolved_name!r} returned no thread.name; "
                "cannot record the primary thread"
            )
        sessions = add_thread_to_session(
            sessions,
            name=resolved_name,
            thread_name=thread_name,
            thread_key=resolved_name,
        )

    registry.save(sessions)
    return sessions[resolved_name]


def list_sessions(registry: SessionRegistry) -> list[Session]:
    """Return the registered sessions sorted by name (dispatcher flagged in each)."""
    sessions = registry.load()
    return [sessions[name] for name in sorted(sessions)]


def disconnect_session(
    config: Config,
    *,
    name: str,
    registry: SessionRegistry,
    sender: ThreadSender | None = None,
    notify: bool = False,
) -> Session:
    """Remove ``name`` from the registry, optionally posting a disconnect note.

    When ``notify`` is true and ``sender`` is provided and the session has a
    primary thread (with a known key), a "session disconnected" note is posted to
    that thread before removal. If the removed session was the dispatcher and
    others remain, one survivor is promoted (handled by
    :func:`claude_google_chat.sessions.remove_session`). Returns the removed
    session record. Raises ``KeyError`` (fail fast) for an unknown name.
    """
    sessions = registry.load()
    if name not in sessions:
        raise KeyError(f"unknown session {name!r}; nothing to disconnect")
    removed = sessions[name]

    if notify and sender is not None:
        primary = removed.primary_thread
        if primary is not None and primary.key is not None:
            sender(_disconnect_message_text(name), primary.key)

    sessions = remove_session(sessions, name)
    registry.save(sessions)
    return removed


def format_session_line(session: Session) -> str:
    """Render one session as a single human-readable line for ``cgc session list``."""
    flag = " [dispatcher]" if session.dispatcher else ""
    thread_count = len(session.threads)
    threads = ", ".join(t.name for t in session.threads) if session.threads else "(none)"
    return f"{session.name}{flag}  space={session.space_id}  threads({thread_count})={threads}"


def connect_summary(session: Session) -> str:
    """Return the routing instructions printed after a successful connect."""
    return routing_instructions(session)
