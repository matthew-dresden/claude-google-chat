"""Session layer on top of the thread primitives (durable, local registry).

A **session** is a named, durable binding between a working context (typically a
git repo + branch + a Claude Code instance) and one or more Google Chat threads
inside a single shared space. It sits on top of the existing thread primitives —
``cgc chat send --thread-key`` (post into a caller-keyed thread, returning the
created ``thread.name``), thread-filtered ``cgc listen`` (emit only in claimed
threads), and ``thread_name`` on each emitted event — and adds:

- A **registry** (``sessions.json``, ``0600``) mapping each session name to its
  space, its claimed threads (``{key, name}``), whether it is the *dispatcher*,
  and when it was created. The registry is the single source of truth for which
  thread belongs to which session.
- **Name routing**: a brand-new (unclaimed) thread whose first line starts with
  ``NAME:`` is *claimed* by that session; a reply in a session's already-claimed
  thread is delivered to it; a thread claimed by another session is skipped.
- A **dispatcher** session that, for a truly-unrouted new thread (no ``NAME:``
  prefix matching any registered session), posts a "which session?" menu so a
  human can pick — without that message being surfaced as work.

Everything here is **pure and injectable** (no network, no implicit disk, no
clock): I/O goes through a :class:`SessionRegistry` protocol (with a file-backed
and an in-memory implementation) and the wall clock is injected. Secrets are
never stored or logged — a session record holds only resource names and labels.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

# Separator between a session name and the rest of a top-level routing message,
# e.g. ``my-session: deploy prod``. The colon is required; surrounding whitespace
# is optional. Single source of truth for both the regex below and the docs.
NAME_ROUTING_SEPARATOR = ":"

# A session name is a short, shell- and thread-key-safe slug: lowercase
# alphanumerics and dashes. Used both to validate an explicit name and as the
# target of :func:`sanitize_session_name` when deriving one.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Matches a leading ``NAME:`` routing prefix (case-insensitive on the name,
# optional surrounding whitespace) capturing the name and the remaining text.
_ROUTING_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9-]*)\s*:\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class SessionThread:
    """A Chat thread claimed by a session.

    Attributes:
        key: The caller-defined ``threadKey`` used to create/route into the
            thread via the incoming webhook (``cgc chat send --thread-key``).
            ``None`` for a thread that was *claimed* from an inbound ``NAME:``
            message rather than created by an outbound send (no key is known).
        name: The stable Chat thread resource name (``spaces/.../threads/...``)
            used to filter inbound messages and to surface the owning thread on
            each emitted event.
    """

    name: str
    key: str | None = None


@dataclass(frozen=True)
class Session:
    """A named binding between a working context and its Chat threads.

    Attributes:
        name: Stable, sanitized session name (the routing target).
        space_id: The shared Chat space (``spaces/...``) the session lives in.
        threads: The threads this session has claimed (primary first).
        dispatcher: Whether this session answers *unrouted* new threads with the
            "which session?" menu. At most one session should be the dispatcher.
        created_at: RFC3339 UTC creation timestamp (injected clock — never the
            ambient wall clock in tests).
    """

    name: str
    space_id: str
    threads: tuple[SessionThread, ...] = ()
    dispatcher: bool = False
    created_at: str | None = None

    @property
    def primary_thread(self) -> SessionThread | None:
        """Return the session's primary (first-claimed) thread, if any."""
        return self.threads[0] if self.threads else None

    def thread_names(self) -> frozenset[str]:
        """Return the set of thread resource names this session has claimed."""
        return frozenset(t.name for t in self.threads)

    def claims_thread(self, thread_name: str) -> bool:
        """Return ``True`` if ``thread_name`` is one of this session's threads."""
        return thread_name in self.thread_names()


@runtime_checkable
class SessionRegistry(Protocol):
    """Loads and persists the session map (injectable for tests, no implicit I/O)."""

    def load(self) -> dict[str, Session]:
        """Return the persisted sessions keyed by name (empty when none exist)."""
        ...

    def save(self, sessions: dict[str, Session]) -> None:
        """Persist the full session map durably (overwrites prior contents)."""
        ...


class InMemorySessionRegistry:
    """Non-durable :class:`SessionRegistry` for unit tests (no disk I/O)."""

    def __init__(self, initial: dict[str, Session] | None = None) -> None:
        self._sessions: dict[str, Session] = dict(initial) if initial else {}

    def load(self) -> dict[str, Session]:
        # Return a copy so callers mutate their own view, mirroring the
        # file-backed store (each load is an independent snapshot).
        return dict(self._sessions)

    def save(self, sessions: dict[str, Session]) -> None:
        self._sessions = dict(sessions)


# JSON shape: ``{"version": "1", "sessions": [ {session...}, ... ]}``. A single
# named top-level key (rather than a bare list) leaves room to evolve the file.
_REGISTRY_VERSION = "1"
_SESSIONS_KEY = "sessions"
_VERSION_KEY = "version"


def _session_to_dict(session: Session) -> dict[str, object]:
    """Serialise a :class:`Session` to a JSON-ready dict (no secrets)."""
    return {
        "name": session.name,
        "space_id": session.space_id,
        "dispatcher": session.dispatcher,
        "created_at": session.created_at,
        "threads": [{"name": t.name, "key": t.key} for t in session.threads],
    }


def _session_from_dict(data: dict[str, object]) -> Session:
    """Parse a :class:`Session` from a registry dict, failing fast on a bad shape."""
    name = data.get("name")
    space_id = data.get("space_id")
    if not isinstance(name, str) or not name:
        raise ValueError(f"invalid session record: missing 'name' in {data!r}")
    if not isinstance(space_id, str) or not space_id:
        raise ValueError(f"invalid session record {name!r}: missing 'space_id'")
    raw_threads = data.get("threads", [])
    if not isinstance(raw_threads, list):
        raise ValueError(f"invalid session record {name!r}: 'threads' must be a list")
    threads: list[SessionThread] = []
    for entry in raw_threads:
        if not isinstance(entry, dict):
            raise ValueError(f"invalid thread entry in session {name!r}: {entry!r}")
        thread_name = entry.get("name")
        if not isinstance(thread_name, str) or not thread_name:
            raise ValueError(f"invalid thread entry in session {name!r}: missing 'name'")
        key = entry.get("key")
        if key is not None and not isinstance(key, str):
            raise ValueError(f"invalid thread key in session {name!r}: {key!r}")
        threads.append(SessionThread(name=thread_name, key=key))
    created_at = data.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        raise ValueError(f"invalid created_at in session {name!r}: {created_at!r}")
    return Session(
        name=name,
        space_id=space_id,
        threads=tuple(threads),
        dispatcher=bool(data.get("dispatcher", False)),
        created_at=created_at,
    )


def serialize_sessions(sessions: dict[str, Session]) -> str:
    """Serialise the session map to a stable, sorted JSON document (pure)."""
    ordered = [_session_to_dict(sessions[name]) for name in sorted(sessions)]
    return json.dumps(
        {_VERSION_KEY: _REGISTRY_VERSION, _SESSIONS_KEY: ordered},
        sort_keys=True,
        indent=2,
    )


def deserialize_sessions(text: str) -> dict[str, Session]:
    """Parse a registry JSON document into a session map, failing fast on bad data.

    Unlike the *state* high-water file (where a corrupt file degrades to a fresh
    start), the session registry is durable user intent: silently dropping it
    would orphan live threads. A malformed document therefore raises ``ValueError``
    so the problem surfaces rather than being masked.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("session registry must be a JSON object")
    raw_sessions = data.get(_SESSIONS_KEY, [])
    if not isinstance(raw_sessions, list):
        raise ValueError("session registry 'sessions' must be a list")
    sessions: dict[str, Session] = {}
    for entry in raw_sessions:
        if not isinstance(entry, dict):
            raise ValueError(f"invalid session entry: {entry!r}")
        session = _session_from_dict(entry)
        sessions[session.name] = session
    return sessions


class FileSessionRegistry:
    """File-backed :class:`SessionRegistry` persisting the map as JSON (``0600``)."""

    def __init__(self, path: Path) -> None:
        """Initialise the registry for the durable sessions file at ``path``."""
        self._path = path

    def load(self) -> dict[str, Session]:
        """Return the persisted sessions, or an empty map for a missing file.

        A *missing* file is an empty registry (no sessions yet). A *present but
        malformed* file fails fast (via :func:`deserialize_sessions`) rather than
        silently discarding durable user intent.
        """
        if not self._path.exists():
            return {}
        return deserialize_sessions(self._path.read_text(encoding="utf-8"))

    def save(self, sessions: dict[str, Session]) -> None:
        """Persist the session map with owner-only (``0600``) permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(serialize_sessions(sessions), encoding="utf-8")
        self._path.chmod(0o600)


def now_rfc3339() -> str:
    """Return the current UTC time as an RFC3339 string (default clock).

    Injectable: callers that need determinism pass their own ``clock`` rather
    than relying on this default, mirroring the rest of the package.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_session_name(raw: str) -> str:
    """Reduce arbitrary text to a safe session-name slug, failing fast if empty.

    Lowercases, replaces any run of non-``[a-z0-9]`` characters with a single
    dash, and trims leading/trailing dashes. The result is a valid thread-key
    and shell-safe token. Raises ``ValueError`` when nothing usable remains (so a
    caller never ends up with an empty or all-separator name).
    """
    lowered = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a session name from {raw!r}; provide an explicit NAME")
    return slug


def validate_session_name(name: str) -> str:
    """Return ``name`` if it is a valid session slug; else raise ``ValueError``.

    The single rule used wherever an explicit session name is accepted, so the
    format check and message live in one place (DRY).
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid session name {name!r}; expected lowercase alphanumerics and dashes "
            "(e.g. 'myrepo-main-ab12')"
        )
    return name


def _short_path_suffix(cwd: str, length: int = 6) -> str:
    """Return a short, stable hex suffix derived deterministically from ``cwd``.

    A truncated SHA-256 of the absolute path. Deterministic (same path → same
    suffix) so reconnecting from the same directory derives the same name, while
    still disambiguating two checkouts of the same repo/branch in different
    directories.
    """
    import hashlib

    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()
    return digest[:length]


def derive_session_name(
    *,
    repo: str | None,
    branch: str | None,
    cwd: str,
) -> str:
    """Derive a stable, sanitized default session name (deterministic).

    The name is ``<repo>-<branch>-<suffix>`` where:

    - ``repo`` is the repository's basename (e.g. the directory name of the git
      toplevel), or ``"repo"`` when not in a git repo;
    - ``branch`` is the current branch, or ``"detached"`` when unavailable;
    - ``suffix`` is a short hex hash of the **absolute cwd** so two checkouts of
      the same repo/branch in different directories get distinct, stable names.

    Each component is sanitized and the whole is re-sanitized so the result is a
    single valid slug. Deterministic: the same ``(repo, branch, cwd)`` always
    yields the same name, which is what makes ``cgc connect`` idempotent.
    """
    repo_part = sanitize_session_name(repo) if repo and repo.strip() else "repo"
    branch_part = sanitize_session_name(branch) if branch and branch.strip() else "detached"
    suffix = _short_path_suffix(cwd)
    return sanitize_session_name(f"{repo_part}-{branch_part}-{suffix}")


def split_name_prefix(text: str) -> tuple[str, str] | None:
    """Split a leading ``NAME:`` routing prefix, returning ``(name, rest)`` or ``None``.

    The name is matched case-insensitively against the slug shape and surrounding
    whitespace is optional. Returns ``None`` when ``text`` has no ``NAME:`` shape
    (so the caller can treat it as an unrouted message). The returned name is
    lowercased (sessions are stored lowercase) and ``rest`` is the surfaced text
    with the prefix stripped.
    """
    match = _ROUTING_RE.match(text or "")
    if match is None:
        return None
    name = match.group(1).lower()
    rest = match.group(2).strip()
    return name, rest


def _has_dispatcher(sessions: dict[str, Session]) -> bool:
    """Return ``True`` if any session is currently the dispatcher."""
    return any(s.dispatcher for s in sessions.values())


def upsert_session(
    sessions: dict[str, Session],
    *,
    name: str,
    space_id: str,
    dispatcher: bool,
    clock: object = now_rfc3339,
) -> dict[str, Session]:
    """Return a new map with session ``name`` created or reused (idempotent).

    If ``name`` already exists its record (threads, created_at) is preserved; the
    explicit ``dispatcher`` request is applied. If it does not exist a fresh
    record is created. Dispatcher election rule: the **first** session ever
    connected auto-becomes the dispatcher when none exists yet, even without
    ``--dispatcher``; an explicit ``dispatcher=True`` demotes any other current
    dispatcher so there is at most one. ``clock`` is the injected timestamp source
    (default :func:`now_rfc3339`).

    Pure: returns a new dict and new Session objects, never mutating the input.
    """
    validate_session_name(name)
    result = dict(sessions)
    make_dispatcher = dispatcher or not _has_dispatcher(result)

    if make_dispatcher:
        # Demote any existing dispatcher so the invariant (at most one) holds.
        for other_name, other in result.items():
            if other.dispatcher and other_name != name:
                result[other_name] = replace(other, dispatcher=False)

    existing = result.get(name)
    if existing is not None:
        result[name] = replace(
            existing,
            space_id=space_id,
            dispatcher=existing.dispatcher or make_dispatcher,
        )
    else:
        created_at = clock() if callable(clock) else None
        result[name] = Session(
            name=name,
            space_id=space_id,
            dispatcher=make_dispatcher,
            created_at=created_at,
        )
    return result


def add_thread_to_session(
    sessions: dict[str, Session],
    *,
    name: str,
    thread_name: str,
    thread_key: str | None = None,
) -> dict[str, Session]:
    """Return a new map with ``thread_name`` claimed under session ``name``.

    Idempotent on the thread *name*: if the session already claims that thread
    the map is returned unchanged (a re-send with the same key, or a re-claim,
    does not duplicate). Raises ``KeyError`` if the session does not exist
    (fail fast — a thread can only be claimed by a known session).

    Pure: never mutates the input map or its sessions.
    """
    if name not in sessions:
        raise KeyError(f"unknown session {name!r}; connect it before claiming a thread")
    session = sessions[name]
    if session.claims_thread(thread_name):
        return dict(sessions)
    result = dict(sessions)
    new_thread = SessionThread(name=thread_name, key=thread_key)
    result[name] = replace(session, threads=(*session.threads, new_thread))
    return result


def remove_session(
    sessions: dict[str, Session],
    name: str,
) -> dict[str, Session]:
    """Return a new map with session ``name`` removed and dispatcher re-elected.

    If the removed session was the dispatcher and other sessions remain, one of
    the survivors (the alphabetically-first, for determinism) is promoted to
    dispatcher so the space is never left without one. Raises ``KeyError`` if the
    session does not exist (fail fast).

    Pure: never mutates the input map.
    """
    if name not in sessions:
        raise KeyError(f"unknown session {name!r}; nothing to disconnect")
    was_dispatcher = sessions[name].dispatcher
    result = {key: value for key, value in sessions.items() if key != name}
    if was_dispatcher and result and not _has_dispatcher(result):
        promote = sorted(result)[0]
        result[promote] = replace(result[promote], dispatcher=True)
    return result


def find_session_claiming_thread(
    sessions: dict[str, Session],
    thread_name: str | None,
) -> Session | None:
    """Return the session that has claimed ``thread_name``, or ``None``.

    ``None`` for an unthreaded message or an unclaimed thread. A thread is owned
    by at most one session (claims are exclusive), so the first match is the
    answer.
    """
    if thread_name is None:
        return None
    for session in sessions.values():
        if session.claims_thread(thread_name):
            return session
    return None


def text_starts_with_any_session_name(
    text: str,
    session_names: Iterable[str],
) -> bool:
    """Return ``True`` if ``text`` begins with ``<name>:`` for any registered name.

    Used by the dispatcher to decide whether a new, unclaimed-thread message is
    *addressed* (and so should be claimed/routed by that session) or *unrouted*
    (and so should get the "which session?" menu). Matching is case-insensitive,
    consistent with :func:`split_name_prefix`.
    """
    split = split_name_prefix(text)
    if split is None:
        return False
    prefix_name, _rest = split
    return prefix_name in {n.lower() for n in session_names}


# Routing decision kinds returned by :func:`route_message`. Each names one of the
# mutually-exclusive outcomes the routing-aware listener acts on.
ROUTE_EMIT = "emit"  # deliver this message to ``session`` as work (text surfaced)
ROUTE_CLAIM = "claim"  # claim the thread for ``session`` AND emit (NAME: stripped)
ROUTE_MENU = "menu"  # dispatcher: post the "which session?" menu, do not emit
ROUTE_SKIP = "skip"  # thread belongs to a different session (or no action)


@dataclass(frozen=True)
class RouteDecision:
    """The outcome of routing one inbound message for a given listening session.

    Attributes:
        action: One of :data:`ROUTE_EMIT`, :data:`ROUTE_CLAIM`, :data:`ROUTE_MENU`,
            or :data:`ROUTE_SKIP`.
        text: The text to surface (for ``emit``/``claim`` the prefix is stripped
            on a claim); empty for non-emitting actions.
        thread_name: The thread the message belongs to (carried through so the
            emitted event / claim records the right thread).
    """

    action: str
    text: str = ""
    thread_name: str | None = None


def route_message(
    sessions: dict[str, Session],
    *,
    listening_session: str,
    thread_name: str | None,
    text: str,
) -> RouteDecision:
    """Decide how the ``listening_session`` should handle one inbound HUMAN message.

    Pure decision function (no I/O, no mutation) shared by the routing-aware
    listener and its tests. Given the current registry, the session doing the
    listening, and the message's thread + text, it returns a :class:`RouteDecision`:

    - **EMIT** — the thread is already claimed by ``listening_session`` (a reply
      in one of my threads): deliver it as work, text unchanged.
    - **CLAIM** — the thread is claimed by *no* session (new/unclaimed) and the
      text starts with ``<listening_session>:``: claim the thread for this
      session and emit, with the ``NAME:`` prefix stripped from the surfaced text.
    - **MENU** — the thread is unclaimed, this session is the dispatcher, and the
      text does **not** start with any registered session name: post the "which
      session?" menu (handled by the caller); do not emit as work.
    - **SKIP** — anything else: a thread claimed by a *different* session, or an
      unclaimed thread addressed to a different (or no) session when this session
      is not the dispatcher.

    The caller performs the side effects (registry claim, menu post, event emit);
    this function only classifies, so it is trivially testable.
    """
    if listening_session not in sessions:
        raise KeyError(f"unknown listening session {listening_session!r}")

    owner = find_session_claiming_thread(sessions, thread_name)
    if owner is not None:
        if owner.name == listening_session:
            return RouteDecision(action=ROUTE_EMIT, text=text, thread_name=thread_name)
        # Claimed by a different session: not ours.
        return RouteDecision(action=ROUTE_SKIP, thread_name=thread_name)

    # Unclaimed thread. Is it addressed to this session by name?
    split = split_name_prefix(text)
    if split is not None:
        prefix_name, rest = split
        if prefix_name == listening_session.lower():
            return RouteDecision(action=ROUTE_CLAIM, text=rest, thread_name=thread_name)

    # Unclaimed and not addressed to us. The dispatcher answers truly-unrouted
    # messages (those not addressed to ANY registered session) with the menu.
    me = sessions[listening_session]
    if me.dispatcher and not text_starts_with_any_session_name(text, sessions.keys()):
        return RouteDecision(action=ROUTE_MENU, thread_name=thread_name)

    return RouteDecision(action=ROUTE_SKIP, thread_name=thread_name)


def dispatcher_menu_text(sessions: dict[str, Session]) -> str:
    """Build the human-readable "which session?" menu the dispatcher posts.

    Lists the registered session names and tells the human how to route: reply in
    a thread, or start with ``NAME:``. Pure (text only) so it is assertable and so
    the caller controls the actual Chat post.
    """
    names = sorted(sessions)
    listed = "\n".join(f"  • {name}" for name in names) if names else "  (none)"
    return (
        "Which session should handle this? Registered sessions:\n"
        f"{listed}\n"
        "Reply with 'NAME: <message>' to address one, or reply inside that "
        "session's thread."
    )


def routing_instructions(session: Session) -> str:
    """Build the routing instructions printed after ``cgc connect``.

    Tells the operator how to talk to this session (reply in its primary thread)
    and how to start a new thread for it (a top-level ``NAME: ...`` message). Pure
    text so it is assertable.
    """
    primary = session.primary_thread
    thread_hint = primary.name if primary is not None else "(no thread yet)"
    return (
        f"Session '{session.name}' connected in space {session.space_id}.\n"
        f"  • Talk to it: reply in its thread {thread_hint}.\n"
        f"  • New thread for it: post a top-level message '{session.name}: <your message>'.\n"
        + (
            "  • This session is the DISPATCHER: it answers unrouted new threads with a menu.\n"
            if session.dispatcher
            else ""
        )
    )
