"""Durable high-water state for the poll loop (restart-resume, no re-emit).

The listener/responder track a ``createTime`` high-water marker so a poll only
fetches messages newer than the last one processed. When that marker lives only
in memory, a restart re-reads recent history and re-emits already-seen messages
(duplicate processing). This module persists the marker to a small JSON file so a
restart **resumes** from where it left off instead of replaying.

The store is defined as a :class:`StateStore` protocol so unit tests can inject
an in-memory implementation (no real disk). :class:`FileStateStore` is the
production implementation, writing the file with owner-only (``0600``)
permissions since the marker reveals activity timing. A malformed or missing
file is treated as "no prior marker" (fresh start), never a crash — losing the
marker only costs a one-time re-read, so failing the whole process would be a
worse outcome than degrading to the in-memory default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

# JSON key under which the high-water ``createTime`` marker is stored. A single
# named key (rather than a bare scalar) leaves room to evolve the file shape.
_MARKER_KEY = "since"


@runtime_checkable
class StateStore(Protocol):
    """Loads and persists the poll high-water marker (injectable for tests)."""

    def load(self) -> str | None:
        """Return the persisted high-water marker, or ``None`` if none exists."""
        ...

    def save(self, marker: str) -> None:
        """Persist ``marker`` as the new high-water marker (durable)."""
        ...


class InMemoryStateStore:
    """Non-durable :class:`StateStore` for unit tests (no disk I/O)."""

    def __init__(self, initial: str | None = None) -> None:
        self._marker = initial

    def load(self) -> str | None:
        return self._marker

    def save(self, marker: str) -> None:
        self._marker = marker


class FileStateStore:
    """File-backed :class:`StateStore` persisting the marker as JSON (0600)."""

    def __init__(self, path: Path) -> None:
        """Initialise the store for the durable state file at ``path``."""
        self._path = path

    def load(self) -> str | None:
        """Return the persisted marker, or ``None`` for a missing/malformed file.

        A missing or unreadable/malformed state file is treated as "no prior
        marker" (fresh start) rather than a fatal error: the only cost is a
        one-time re-read of recent history, whereas crashing the listener on a
        corrupt cache would be strictly worse.
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        marker = data.get(_MARKER_KEY)
        return marker if isinstance(marker, str) and marker else None

    def save(self, marker: str) -> None:
        """Persist ``marker`` to the state file with owner-only permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({_MARKER_KEY: marker}, sort_keys=True),
            encoding="utf-8",
        )
        self._path.chmod(0o600)
