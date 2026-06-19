"""Tests for the structured message format (parse/format round-trips)."""

from __future__ import annotations

import json

import pytest

from claude_google_chat.messages import (
    ALLOWED_STATUSES,
    STATUS_EMOJI,
    ChatMessage,
    format_message,
    parse_message,
)


def test_format_round_trips_status() -> None:
    msg = ChatMessage(
        kind="status",
        status="success",
        text="Tests passed",
        ts="2026-06-19T12:00:00Z",
        correlation_id="abc-123",
    )
    wire = format_message(msg)
    parsed = parse_message(wire)
    assert parsed.kind == "status"
    assert parsed.status == "success"
    assert parsed.text == "Tests passed"
    assert parsed.ts == "2026-06-19T12:00:00Z"
    assert parsed.correlation_id == "abc-123"
    assert parsed.version == "1"


def test_parse_trigger_line() -> None:
    parsed = parse_message("claude-command: deploy prod --force")
    assert parsed.kind == "command"
    assert parsed.command == "deploy"
    assert parsed.args == ["prod", "--force"]


def test_parse_rejects_bad_version() -> None:
    envelope = json.dumps({"version": "2", "kind": "status", "status": "info", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "version" in str(exc_info.value).lower()


def test_parse_rejects_unknown_status() -> None:
    envelope = json.dumps({"version": "1", "kind": "status", "status": "halfway", "text": "x"})
    with pytest.raises(ValueError):
        parse_message(envelope)


def test_status_emoji_mapping_covers_all_statuses() -> None:
    for status in ALLOWED_STATUSES:
        assert status in STATUS_EMOJI
        assert STATUS_EMOJI[status]


def test_custom_trigger_prefix() -> None:
    parsed = parse_message("bot-command: ship now", trigger_prefix="bot-command:")
    assert parsed.kind == "command"
    assert parsed.command == "ship"
    assert parsed.args == ["now"]


def test_result_kind_round_trips() -> None:
    msg = ChatMessage(
        kind="result",
        status="error",
        text="Build failed",
        ts="2026-06-19T13:30:00Z",
    )
    parsed = parse_message(format_message(msg))
    assert parsed.kind == "result"
    assert parsed.status == "error"
    assert parsed.text == "Build failed"
