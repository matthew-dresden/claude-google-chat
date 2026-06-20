"""Tests for the pure session primitives (registry, naming, routing).

These exercise :mod:`claude_google_chat.sessions` in isolation — no network, no
disk (the file-backed registry is driven against ``tmp_path``), no real clock
(an injected timestamp factory). Each test asserts observable behaviour that can
actually fail if the logic is wrong: serialization round-trips, deterministic
name derivation, idempotent upserts, dispatcher election/promotion, exclusive
thread claims, the ``NAME:`` prefix split, and every branch of the routing
decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_google_chat.sessions import (
    ROUTE_CLAIM,
    ROUTE_EMIT,
    ROUTE_MENU,
    ROUTE_SKIP,
    FileSessionRegistry,
    InMemorySessionRegistry,
    Session,
    SessionThread,
    add_thread_to_session,
    derive_session_name,
    deserialize_sessions,
    dispatcher_menu_text,
    find_session_claiming_thread,
    remove_session,
    route_message,
    routing_instructions,
    sanitize_session_name,
    serialize_sessions,
    split_name_prefix,
    text_starts_with_any_session_name,
    upsert_session,
    validate_session_name,
)

SPACE = "spaces/AAAA"
FROZEN = "2026-06-20T12:00:00Z"


def _clock() -> str:
    """A deterministic injected clock for created_at timestamps."""
    return FROZEN


def _session(
    name: str,
    *,
    threads: tuple[SessionThread, ...] = (),
    dispatcher: bool = False,
) -> Session:
    return Session(
        name=name,
        space_id=SPACE,
        threads=threads,
        dispatcher=dispatcher,
        created_at=FROZEN,
    )


# --------------------------------------------------------------------------- #
# Name derivation / sanitization / validation.
# --------------------------------------------------------------------------- #


def test_sanitize_lowercases_and_dashes_runs() -> None:
    assert sanitize_session_name("My Repo/Feature_Branch!") == "my-repo-feature-branch"


def test_sanitize_empty_fails_fast() -> None:
    with pytest.raises(ValueError):
        sanitize_session_name("  !!! ")


def test_validate_rejects_uppercase_and_underscore() -> None:
    with pytest.raises(ValueError):
        validate_session_name("Bad_Name")


def test_validate_accepts_slug() -> None:
    assert validate_session_name("myrepo-main-ab12cd") == "myrepo-main-ab12cd"


def test_derive_is_deterministic_for_same_inputs() -> None:
    a = derive_session_name(repo="myrepo", branch="main", cwd="/work/checkout-a")
    b = derive_session_name(repo="myrepo", branch="main", cwd="/work/checkout-a")
    assert a == b
    assert a.startswith("myrepo-main-")


def test_derive_differs_for_different_cwd_same_repo_branch() -> None:
    a = derive_session_name(repo="myrepo", branch="main", cwd="/work/checkout-a")
    b = derive_session_name(repo="myrepo", branch="main", cwd="/work/checkout-b")
    assert a != b


def test_derive_defaults_when_not_a_repo() -> None:
    name = derive_session_name(repo=None, branch=None, cwd="/tmp/x")
    assert name.startswith("repo-detached-")
    # The result is a valid slug.
    assert validate_session_name(name) == name


# --------------------------------------------------------------------------- #
# Serialization round-trip + file registry.
# --------------------------------------------------------------------------- #


def test_serialize_deserialize_round_trips() -> None:
    sessions = {
        "alpha": _session(
            "alpha",
            threads=(SessionThread(name=f"{SPACE}/threads/T1", key="alpha"),),
            dispatcher=True,
        ),
        "beta": _session("beta"),
    }
    restored = deserialize_sessions(serialize_sessions(sessions))
    assert restored == sessions


def test_deserialize_rejects_missing_space() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions('{"sessions": [{"name": "x"}]}')


def test_deserialize_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions("[]")


def test_deserialize_rejects_non_list_sessions() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions('{"sessions": {}}')


def test_deserialize_rejects_non_dict_entry() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions('{"sessions": ["nope"]}')


def test_deserialize_rejects_missing_name() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions('{"sessions": [{"space_id": "spaces/AAAA"}]}')


def test_deserialize_rejects_non_list_threads() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions(
            '{"sessions": [{"name": "a", "space_id": "spaces/AAAA", "threads": 1}]}'
        )


def test_deserialize_rejects_bad_thread_entry() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions(
            '{"sessions": [{"name": "a", "space_id": "spaces/AAAA", "threads": ["x"]}]}'
        )


def test_deserialize_rejects_thread_missing_name() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions(
            '{"sessions": [{"name": "a", "space_id": "spaces/AAAA", "threads": [{"key": "k"}]}]}'
        )


def test_deserialize_rejects_non_string_thread_key() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions(
            '{"sessions": [{"name": "a", "space_id": "spaces/AAAA", '
            '"threads": [{"name": "spaces/AAAA/threads/T", "key": 1}]}]}'
        )


def test_deserialize_rejects_non_string_created_at() -> None:
    with pytest.raises(ValueError):
        deserialize_sessions(
            '{"sessions": [{"name": "a", "space_id": "spaces/AAAA", "created_at": 1}]}'
        )


def test_file_registry_missing_file_is_empty(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions.json")
    assert registry.load() == {}


def test_file_registry_save_load_round_trip_and_0600(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sessions.json"
    registry = FileSessionRegistry(path)
    sessions = {"alpha": _session("alpha", dispatcher=True)}
    registry.save(sessions)

    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert registry.load() == sessions


def test_file_registry_corrupt_file_fails_fast(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        FileSessionRegistry(path).load()


def test_in_memory_registry_is_isolated_snapshot() -> None:
    registry = InMemorySessionRegistry({"a": _session("a")})
    loaded = registry.load()
    loaded["a"] = _session("a", dispatcher=True)
    # Mutating the returned dict does not change the store until save().
    assert registry.load()["a"].dispatcher is False


# --------------------------------------------------------------------------- #
# upsert / dispatcher election / promotion / claims.
# --------------------------------------------------------------------------- #


def test_first_session_auto_becomes_dispatcher() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    assert sessions["alpha"].dispatcher is True
    assert sessions["alpha"].created_at == FROZEN


def test_second_session_is_not_dispatcher_by_default() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = upsert_session(sessions, name="beta", space_id=SPACE, dispatcher=False, clock=_clock)
    assert sessions["alpha"].dispatcher is True
    assert sessions["beta"].dispatcher is False


def test_explicit_dispatcher_demotes_previous() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = upsert_session(sessions, name="beta", space_id=SPACE, dispatcher=True, clock=_clock)
    assert sessions["alpha"].dispatcher is False
    assert sessions["beta"].dispatcher is True
    # Exactly one dispatcher.
    assert sum(1 for s in sessions.values() if s.dispatcher) == 1


def test_upsert_is_idempotent_and_preserves_threads() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = add_thread_to_session(
        sessions, name="alpha", thread_name=f"{SPACE}/threads/T1", thread_key="alpha"
    )
    # Reconnect: same name, threads preserved, no duplication.
    sessions = upsert_session(
        sessions, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock
    )
    assert sessions["alpha"].threads == (SessionThread(name=f"{SPACE}/threads/T1", key="alpha"),)


def test_add_thread_is_idempotent_on_name() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = add_thread_to_session(sessions, name="alpha", thread_name=f"{SPACE}/threads/T1")
    again = add_thread_to_session(sessions, name="alpha", thread_name=f"{SPACE}/threads/T1")
    assert again["alpha"].threads == sessions["alpha"].threads


def test_add_thread_unknown_session_fails_fast() -> None:
    with pytest.raises(KeyError):
        add_thread_to_session({}, name="ghost", thread_name=f"{SPACE}/threads/T1")


def test_remove_promotes_new_dispatcher() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = upsert_session(sessions, name="beta", space_id=SPACE, dispatcher=False, clock=_clock)
    # alpha is dispatcher; removing it promotes beta.
    sessions = remove_session(sessions, "alpha")
    assert "alpha" not in sessions
    assert sessions["beta"].dispatcher is True


def test_remove_non_dispatcher_leaves_dispatcher_unchanged() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = upsert_session(sessions, name="beta", space_id=SPACE, dispatcher=False, clock=_clock)
    sessions = remove_session(sessions, "beta")
    assert sessions["alpha"].dispatcher is True


def test_remove_last_session_leaves_empty_registry() -> None:
    sessions = upsert_session({}, name="alpha", space_id=SPACE, dispatcher=False, clock=_clock)
    assert remove_session(sessions, "alpha") == {}


def test_remove_unknown_fails_fast() -> None:
    with pytest.raises(KeyError):
        remove_session({}, "ghost")


# --------------------------------------------------------------------------- #
# Prefix splitting + claim lookup.
# --------------------------------------------------------------------------- #


def test_split_name_prefix_strips_and_lowercases() -> None:
    assert split_name_prefix("Alpha: deploy prod") == ("alpha", "deploy prod")


def test_split_name_prefix_optional_space() -> None:
    assert split_name_prefix("alpha:deploy") == ("alpha", "deploy")


def test_split_name_prefix_none_when_no_colon() -> None:
    assert split_name_prefix("just chatting") is None


def test_find_session_claiming_thread() -> None:
    sessions = {
        "alpha": _session("alpha", threads=(SessionThread(name=f"{SPACE}/threads/T1"),)),
    }
    found = find_session_claiming_thread(sessions, f"{SPACE}/threads/T1")
    assert found is not None and found.name == "alpha"
    assert find_session_claiming_thread(sessions, f"{SPACE}/threads/other") is None
    assert find_session_claiming_thread(sessions, None) is None


def test_text_starts_with_any_session_name() -> None:
    assert text_starts_with_any_session_name("Alpha: hi", ["alpha", "beta"]) is True
    assert text_starts_with_any_session_name("gamma: hi", ["alpha", "beta"]) is False
    assert text_starts_with_any_session_name("no prefix", ["alpha"]) is False


# --------------------------------------------------------------------------- #
# route_message — the routing decision matrix.
# --------------------------------------------------------------------------- #


def _two_sessions() -> dict[str, Session]:
    """alpha (dispatcher, claims T1) + beta (claims T2)."""
    return {
        "alpha": _session(
            "alpha",
            threads=(SessionThread(name=f"{SPACE}/threads/T1"),),
            dispatcher=True,
        ),
        "beta": _session(
            "beta",
            threads=(SessionThread(name=f"{SPACE}/threads/T2"),),
        ),
    }


def test_route_reply_in_own_thread_emits() -> None:
    decision = route_message(
        _two_sessions(),
        listening_session="alpha",
        thread_name=f"{SPACE}/threads/T1",
        text="restart please",
    )
    assert decision.action == ROUTE_EMIT
    assert decision.text == "restart please"
    assert decision.thread_name == f"{SPACE}/threads/T1"


def test_route_thread_claimed_by_other_is_skipped() -> None:
    decision = route_message(
        _two_sessions(),
        listening_session="alpha",
        thread_name=f"{SPACE}/threads/T2",
        text="hello",
    )
    assert decision.action == ROUTE_SKIP


def test_route_named_new_thread_is_claimed_and_prefix_stripped() -> None:
    decision = route_message(
        _two_sessions(),
        listening_session="beta",
        thread_name=f"{SPACE}/threads/NEW",
        text="beta: run the migration",
    )
    assert decision.action == ROUTE_CLAIM
    assert decision.text == "run the migration"
    assert decision.thread_name == f"{SPACE}/threads/NEW"


def test_route_dispatcher_posts_menu_for_unrouted_new_thread() -> None:
    decision = route_message(
        _two_sessions(),
        listening_session="alpha",  # dispatcher
        thread_name=f"{SPACE}/threads/NEW",
        text="hey is anyone there",  # no NAME: prefix
    )
    assert decision.action == ROUTE_MENU


def test_route_dispatcher_does_not_menu_for_named_message() -> None:
    # Addressed to beta in a new thread; alpha (dispatcher) listening must NOT
    # menu (it is routed to another session by name) and must NOT claim it.
    decision = route_message(
        _two_sessions(),
        listening_session="alpha",
        thread_name=f"{SPACE}/threads/NEW",
        text="beta: do the thing",
    )
    assert decision.action == ROUTE_SKIP


def test_route_non_dispatcher_unrouted_message_is_skipped() -> None:
    decision = route_message(
        _two_sessions(),
        listening_session="beta",  # not dispatcher
        thread_name=f"{SPACE}/threads/NEW",
        text="random text no prefix",
    )
    assert decision.action == ROUTE_SKIP


def test_route_unknown_listening_session_fails_fast() -> None:
    with pytest.raises(KeyError):
        route_message(
            _two_sessions(),
            listening_session="ghost",
            thread_name=f"{SPACE}/threads/NEW",
            text="hi",
        )


# --------------------------------------------------------------------------- #
# Menu + instructions text.
# --------------------------------------------------------------------------- #


def test_dispatcher_menu_lists_session_names() -> None:
    text = dispatcher_menu_text(_two_sessions())
    assert "alpha" in text
    assert "beta" in text
    assert "NAME:" in text


def test_routing_instructions_mention_thread_and_top_level() -> None:
    session = _session(
        "alpha",
        threads=(SessionThread(name=f"{SPACE}/threads/T1", key="alpha"),),
        dispatcher=True,
    )
    text = routing_instructions(session)
    assert f"{SPACE}/threads/T1" in text
    assert "alpha: <your message>" in text
    assert "DISPATCHER" in text
