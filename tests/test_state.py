"""Tests for the durable high-water :class:`StateStore` implementations.

These exercise the file-backed store against a real ``tmp_path`` file (never the
OS config directory) to prove the marker round-trips, is written with owner-only
permissions, and degrades to "no marker" (fresh start) rather than crashing on a
missing or corrupt file. The in-memory store is covered as the injectable test
double used by the loop tests.
"""

from __future__ import annotations

from pathlib import Path

from claude_google_chat.state import FileStateStore, InMemoryStateStore, StateStore


def test_in_memory_store_round_trips() -> None:
    store = InMemoryStateStore()
    assert store.load() is None
    store.save("2026-06-20T12:00:00Z")
    assert store.load() == "2026-06-20T12:00:00Z"


def test_in_memory_store_seeds_initial_marker() -> None:
    store = InMemoryStateStore(initial="2026-06-20T11:00:00Z")
    assert store.load() == "2026-06-20T11:00:00Z"


def test_in_memory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryStateStore(), StateStore)


def test_file_store_missing_file_loads_none(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "missing" / "listen-state.json")
    assert store.load() is None


def test_file_store_round_trips_marker(tmp_path: Path) -> None:
    path = tmp_path / "listen-state.json"
    store = FileStateStore(path)
    store.save("2026-06-20T12:34:56Z")
    # A fresh store instance reads the persisted value (simulates a restart).
    assert FileStateStore(path).load() == "2026-06-20T12:34:56Z"


def test_file_store_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "listen-state.json"
    FileStateStore(path).save("2026-06-20T12:00:00Z")
    assert path.exists()


def test_file_store_writes_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "listen-state.json"
    FileStateStore(path).save("2026-06-20T12:00:00Z")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_file_store_corrupt_json_loads_none(tmp_path: Path) -> None:
    path = tmp_path / "listen-state.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert FileStateStore(path).load() is None


def test_file_store_non_object_json_loads_none(tmp_path: Path) -> None:
    path = tmp_path / "listen-state.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    assert FileStateStore(path).load() is None


def test_file_store_missing_marker_key_loads_none(tmp_path: Path) -> None:
    path = tmp_path / "listen-state.json"
    path.write_text('{"other": "value"}', encoding="utf-8")
    assert FileStateStore(path).load() is None
