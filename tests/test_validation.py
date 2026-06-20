"""Unit tests for :mod:`claude_google_chat.validation`.

The module is pure (no I/O) and is the single source of truth for the space-id
and RFC3339 ``createTime`` format checks used by ``chat.py``. These exercise both
the accept and the fail-fast reject paths.
"""

from __future__ import annotations

import pytest

from claude_google_chat.validation import validate_create_time, validate_space_id


@pytest.mark.parametrize(
    "space_id",
    ["spaces/AAAA", "spaces/AbC-123_xyz"],
)
def test_validate_space_id_accepts_well_formed(space_id: str) -> None:
    assert validate_space_id(space_id) == space_id


@pytest.mark.parametrize(
    "space_id",
    ["AAAA", "spaces/", "spaces/AA/extra", "space/AAAA", "spaces/AA AA", ""],
)
def test_validate_space_id_rejects_malformed(space_id: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_space_id(space_id)
    assert "space" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "since",
    [
        "2026-06-20T12:34:56Z",
        "2026-06-20T12:34:56.123456Z",
        "2026-06-20T12:34:56+00:00",
        "2026-06-20T12:34:56-05:30",
    ],
)
def test_validate_create_time_accepts_rfc3339(since: str) -> None:
    assert validate_create_time(since) == since


@pytest.mark.parametrize(
    "since",
    [
        "not-a-timestamp",
        "2026-06-20",
        '2026" OR createTime > "1970',
        "2026-06-20T12:34:56",  # missing zone
        "",
    ],
)
def test_validate_create_time_rejects_malformed(since: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_create_time(since)
    assert "createTime" in str(exc_info.value)
