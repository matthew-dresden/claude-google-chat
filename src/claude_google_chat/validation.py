"""Shared input validators (pure, no I/O).

Single source of truth for the format checks that more than one module needs:
the Chat space resource id and the RFC3339 ``createTime`` lower bound used in
Chat API list filters. Keeping these here avoids duplicated regexes and error
strings drifting across ``chat.py`` and ``bootstrap.py`` (DRY), and lets a
malformed value fail fast with one consistent, actionable message.
"""

from __future__ import annotations

import re

# Chat space resource ids look like ``spaces/AAAA...``.
_SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")

# RFC3339 / Chat ``createTime`` shape, e.g. ``2026-06-20T12:34:56.123456Z`` or
# ``2026-06-20T12:34:56+00:00``. Validated before interpolation into a Chat API
# ``filter`` expression so an unexpected value fails fast instead of being
# injected verbatim.
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def validate_space_id(space_id: str) -> str:
    """Return ``space_id`` if it matches ``spaces/<id>``; else raise ``ValueError``.

    The single, testable rule used by every call site that accepts a space id,
    so the format check and error message exist in exactly one place.
    """
    if not _SPACE_RE.match(space_id):
        raise ValueError(f"invalid space id {space_id!r}; expected form 'spaces/<id>'")
    return space_id


def validate_create_time(since: str) -> str:
    """Return ``since`` if it is a well-formed RFC3339 timestamp; else raise.

    Used to guard the value interpolated into the Chat API ``createTime``
    ``filter`` expression so a malformed/unexpected timestamp fails fast rather
    than being injected verbatim into the query.
    """
    if not _RFC3339_RE.match(since):
        raise ValueError(
            f"invalid createTime filter value {since!r}; expected an RFC3339 timestamp "
            "such as '2026-06-20T12:34:56Z'"
        )
    return since
