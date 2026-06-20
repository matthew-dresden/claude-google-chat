"""Tests for the connect/list/disconnect orchestration (`sessionops`).

The registry, the threaded sender, the clock, and the git context are all
injected, so these run with no network, no disk, and no real clock. They assert
that connect creates+registers a primary thread idempotently, derives a sane
default name, that the first session auto-becomes the dispatcher, that list
reflects the registry, and that disconnect removes a session and (optionally)
notifies + promotes a new dispatcher.
"""

from __future__ import annotations

from typing import Any

import pytest

from claude_google_chat.config import Config
from claude_google_chat.sessionops import (
    connect_session,
    disconnect_session,
    format_session_line,
    list_sessions,
    resolve_session_name,
)
from claude_google_chat.sessions import (
    InMemorySessionRegistry,
    Session,
    SessionThread,
)

SPACE = "spaces/AAAA"
FROZEN = "2026-06-20T12:00:00Z"


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {"space_id": SPACE, "webhook_url": "https://example/webhook"}
    base.update(overrides)
    return Config(**base)


class _RecordingSender:
    """A fake (text, thread_key) -> thread.name sender recording its calls."""

    def __init__(self, thread_name: str = f"{SPACE}/threads/T-new") -> None:
        self._thread_name = thread_name
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, thread_key: str) -> str:
        self.calls.append((text, thread_key))
        # Each distinct key maps to its own stable thread name (mirrors the
        # webhook's REPLY_FALLBACK_TO_NEW_THREAD behaviour).
        return f"{SPACE}/threads/{thread_key}"


def _clock() -> str:
    return FROZEN


# --------------------------------------------------------------------------- #
# resolve_session_name.
# --------------------------------------------------------------------------- #


def test_resolve_uses_explicit_name() -> None:
    name = resolve_session_name(
        "my-explicit",
        cwd="/x",
        repo_name=lambda _c: "ignored",
        branch_name=lambda _c: "ignored",
    )
    assert name == "my-explicit"


def test_resolve_explicit_invalid_fails_fast() -> None:
    with pytest.raises(ValueError):
        resolve_session_name("Bad Name", cwd="/x")


def test_resolve_derives_from_git_context() -> None:
    name = resolve_session_name(
        None,
        cwd="/work/checkout",
        repo_name=lambda _c: "myrepo",
        branch_name=lambda _c: "feature/x",
    )
    assert name.startswith("myrepo-feature-x-")


# --------------------------------------------------------------------------- #
# connect_session.
# --------------------------------------------------------------------------- #


def test_connect_creates_thread_registers_and_auto_dispatcher() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()

    session = connect_session(
        _config(),
        name="alpha",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )

    # The opening message was sent once, keyed by the session name.
    assert len(sender.calls) == 1
    _text, key = sender.calls[0]
    assert key == "alpha"

    # The primary thread is recorded, and the first session is the dispatcher.
    assert session.primary_thread is not None
    assert session.primary_thread.name == f"{SPACE}/threads/alpha"
    assert session.primary_thread.key == "alpha"
    assert session.dispatcher is True
    assert session.created_at == FROZEN
    # Persisted to the registry.
    assert registry.load()["alpha"].primary_thread is not None


def test_connect_is_idempotent_no_duplicate_thread() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    common = dict(
        name="alpha",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    connect_session(_config(), **common)
    session = connect_session(_config(), **common)

    # Second connect reuses the existing thread: no second send, one thread.
    assert len(sender.calls) == 1
    assert len(session.threads) == 1


def test_connect_derives_default_name_when_omitted() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    session = connect_session(
        _config(),
        name=None,
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/work/checkout",
        clock=_clock,
        repo_name=lambda _c: "myrepo",
        branch_name=lambda _c: "main",
    )
    assert session.name.startswith("myrepo-main-")


def test_connect_explicit_space_overrides_config() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    session = connect_session(
        _config(space_id="spaces/CONFIG"),
        name="alpha",
        space_id="spaces/OVERRIDE",
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    assert session.space_id == "spaces/OVERRIDE"


def test_connect_no_space_anywhere_fails_fast() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    with pytest.raises(ValueError):
        connect_session(
            _config(space_id=None),
            name="alpha",
            space_id=None,
            dispatcher=False,
            registry=registry,
            sender=sender,
            cwd="/x",
            clock=_clock,
        )


def test_connect_explicit_dispatcher_flag() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    # Pre-seed a non-dispatcher session so alpha is not auto-elected.
    registry.save(
        {
            "beta": Session(
                name="beta",
                space_id=SPACE,
                threads=(SessionThread(name=f"{SPACE}/threads/T-beta", key="beta"),),
                dispatcher=True,
                created_at=FROZEN,
            )
        }
    )
    session = connect_session(
        _config(),
        name="alpha",
        space_id=None,
        dispatcher=True,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    assert session.dispatcher is True
    # beta was demoted (exactly one dispatcher).
    assert registry.load()["beta"].dispatcher is False


# --------------------------------------------------------------------------- #
# list_sessions / format.
# --------------------------------------------------------------------------- #


def test_list_returns_sorted_sessions() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    connect_session(
        _config(),
        name="zeta",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    connect_session(
        _config(),
        name="alpha",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    names = [s.name for s in list_sessions(registry)]
    assert names == ["alpha", "zeta"]


def test_format_session_line_flags_dispatcher_and_threads() -> None:
    session = Session(
        name="alpha",
        space_id=SPACE,
        threads=(SessionThread(name=f"{SPACE}/threads/T1", key="alpha"),),
        dispatcher=True,
        created_at=FROZEN,
    )
    line = format_session_line(session)
    assert "alpha" in line
    assert "[dispatcher]" in line
    assert f"{SPACE}/threads/T1" in line


# --------------------------------------------------------------------------- #
# disconnect_session.
# --------------------------------------------------------------------------- #


def test_disconnect_removes_and_promotes() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    connect_session(
        _config(),
        name="alpha",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )  # alpha auto-dispatcher
    connect_session(
        _config(),
        name="beta",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )

    disconnect_session(_config(), name="alpha", registry=registry)
    sessions = registry.load()
    assert "alpha" not in sessions
    assert sessions["beta"].dispatcher is True


def test_disconnect_notify_posts_to_primary_thread() -> None:
    registry = InMemorySessionRegistry()
    sender = _RecordingSender()
    connect_session(
        _config(),
        name="alpha",
        space_id=None,
        dispatcher=False,
        registry=registry,
        sender=sender,
        cwd="/x",
        clock=_clock,
    )
    sender.calls.clear()

    disconnect_session(_config(), name="alpha", registry=registry, sender=sender, notify=True)
    # A disconnect note was posted into the session's primary thread key.
    assert len(sender.calls) == 1
    text, key = sender.calls[0]
    assert key == "alpha"
    assert "disconnected" in text


def test_disconnect_unknown_fails_fast() -> None:
    registry = InMemorySessionRegistry()
    with pytest.raises(KeyError):
        disconnect_session(_config(), name="ghost", registry=registry)


def test_connect_empty_thread_name_fails_fast() -> None:
    """A sender that returns no thread.name fails fast (cannot bind the thread)."""
    registry = InMemorySessionRegistry()

    def _empty_sender(text: str, thread_key: str) -> str:
        return ""

    with pytest.raises(ValueError):
        connect_session(
            _config(),
            name="alpha",
            space_id=None,
            dispatcher=False,
            registry=registry,
            sender=_empty_sender,
            cwd="/x",
            clock=_clock,
        )


# --------------------------------------------------------------------------- #
# Git context helpers (real temp repo; no mocking of git).
# --------------------------------------------------------------------------- #


def _init_repo(repo: Any, branch: str) -> None:
    """Initialise a real git repo on ``branch`` with one commit (born branch)."""
    import subprocess

    def _run(args: list[str]) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    _run(["git", "init", "-b", branch])
    _run(["git", "config", "user.email", "t@example.com"])
    _run(["git", "config", "user.name", "Test"])
    _run(["git", "commit", "--allow-empty", "-m", "init"])


def test_git_repo_and_branch_from_real_repo(tmp_path: Any) -> None:
    """The git helpers read the toplevel basename and branch from a real repo."""
    repo = tmp_path / "myproj"
    repo.mkdir()
    _init_repo(repo, "trunk")

    from claude_google_chat.sessionops import git_branch_name, git_repo_name

    assert git_repo_name(str(repo)) == "myproj"
    assert git_branch_name(str(repo)) == "trunk"


def test_git_helpers_return_none_outside_repo(tmp_path: Any) -> None:
    """Outside a git repo the helpers degrade to None (caller uses defaults)."""
    from claude_google_chat.sessionops import git_branch_name, git_repo_name

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert git_repo_name(str(plain)) is None
    assert git_branch_name(str(plain)) is None


def test_git_helpers_degrade_when_git_absent(monkeypatch: Any) -> None:
    """A missing git binary (OSError) degrades to None, never a crash."""
    import claude_google_chat.sessionops as ops

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(ops.subprocess, "run", _boom)
    assert ops.git_repo_name("/x") is None
    assert ops.git_branch_name("/x") is None


def test_resolve_uses_real_git_default_callables(tmp_path: Any) -> None:
    """resolve_session_name with default git callables derives from a real repo."""
    repo = tmp_path / "proj2"
    repo.mkdir()
    _init_repo(repo, "main")

    name = resolve_session_name(None, cwd=str(repo))
    assert name.startswith("proj2-main-")
