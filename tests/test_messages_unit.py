"""Exhaustive unit tests for :mod:`claude_google_chat.messages`.

The module is pure (no I/O), so these tests exercise the full structured
ChatOps envelope contract directly:

- ``format_message``: the on-the-wire form (emoji-prefixed summary line, the
  fenced JSON envelope, timestamp population, sorted/stable keys) and the
  status -> emoji mapping that drives the visible "title" decoration.
- ``parse_message``: trigger-prefix detection (the ``claude:`` command
  form), custom prefixes, fenced and bare JSON envelopes, and every error path
  (None input, empty command, neither-trigger-nor-envelope, malformed JSON).
- ``_validate``: kind/status allow-lists and the kind-specific required fields.
- Round-trips across all kinds, proving format/parse are inverse for the fields
  that survive the wire.

Time is pinned with the ``frozen_clock`` fixture so the auto-populated timestamp
is deterministic and assertable.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from claude_google_chat.messages import (
    ALLOWED_KINDS,
    ALLOWED_STATUSES,
    DEFAULT_TRIGGER_PREFIX,
    ENVELOPE_VERSION,
    STATUS_EMOJI,
    ChatMessage,
    format_message,
    parse_message,
)

# --------------------------------------------------------------------------- #
# Constants / invariants.
# --------------------------------------------------------------------------- #


def test_default_trigger_prefix_value() -> None:
    assert DEFAULT_TRIGGER_PREFIX == "claude:"


def test_status_emoji_covers_exactly_allowed_statuses() -> None:
    assert set(STATUS_EMOJI) == set(ALLOWED_STATUSES)
    for status in ALLOWED_STATUSES:
        assert STATUS_EMOJI[status], status


def test_allowed_kinds_and_statuses_are_frozen() -> None:
    assert isinstance(ALLOWED_KINDS, frozenset)
    assert isinstance(ALLOWED_STATUSES, frozenset)
    assert ALLOWED_KINDS == {"status", "command", "result"}


# --------------------------------------------------------------------------- #
# format_message: summary line (emoji title) + fenced envelope.
# --------------------------------------------------------------------------- #


def test_format_summary_line_has_status_emoji_then_text() -> None:
    msg = ChatMessage(
        kind="status", status="success", text="Tests passed", ts="2026-06-20T12:00:00Z"
    )
    wire = format_message(msg)
    summary = wire.splitlines()[0]
    assert summary == f"{STATUS_EMOJI['success']} Tests passed"


def test_format_each_status_uses_its_emoji() -> None:
    for status in ALLOWED_STATUSES:
        msg = ChatMessage(kind="status", status=status, text="hi", ts="2026-06-20T12:00:00Z")
        summary = format_message(msg).splitlines()[0]
        assert summary.startswith(STATUS_EMOJI[status])


def test_format_command_without_status_has_no_emoji_prefix() -> None:
    """A statusless command summary is just the trimmed text (no emoji)."""
    msg = ChatMessage(
        kind="command", command="deploy", text="deploy prod", ts="2026-06-20T12:00:00Z"
    )
    summary = format_message(msg).splitlines()[0]
    assert summary == "deploy prod"


def test_format_without_envelope_returns_summary_only() -> None:
    """``include_envelope=False`` yields the clean summary line and no fenced JSON."""
    msg = ChatMessage(
        kind="status", status="success", text="Tests passed", ts="2026-06-20T12:00:00Z"
    )
    wire = format_message(msg, include_envelope=False)
    assert wire == f"{STATUS_EMOJI['success']} Tests passed"
    assert "```" not in wire


def test_format_without_envelope_still_validates() -> None:
    """The summary-only path validates first, failing fast on a bad message."""
    msg = ChatMessage(kind="status", status=None, text="x", ts="2026-06-20T12:00:00Z")
    with pytest.raises(ValueError):
        format_message(msg, include_envelope=False)


def test_format_default_includes_envelope() -> None:
    """The default keeps the fenced JSON envelope (machine/log callers)."""
    msg = ChatMessage(kind="status", status="info", text="x", ts="2026-06-20T12:00:00Z")
    assert "```" in format_message(msg)


def test_format_without_envelope_matches_enveloped_summary_line(frozen_clock: str) -> None:
    """The summary-only output equals the first line of the enveloped output."""
    msg = ChatMessage(kind="status", status="info", text="x", ts="2026-06-20T12:00:00Z")
    summary = format_message(msg, include_envelope=False)
    enveloped = format_message(msg, include_envelope=True)
    assert summary == enveloped.splitlines()[0]
    assert "```" not in summary


def test_format_wraps_envelope_in_fenced_code_block() -> None:
    msg = ChatMessage(kind="status", status="info", text="x", ts="2026-06-20T12:00:00Z")
    wire = format_message(msg)
    fences = [line for line in wire.splitlines() if line.strip() == "```"]
    assert len(fences) == 2


def test_format_envelope_contains_all_fields() -> None:
    msg = ChatMessage(
        kind="result",
        status="error",
        text="Build failed",
        command="build",
        args=["--clean"],
        ts="2026-06-20T12:00:00Z",
        correlation_id="corr-9",
    )
    wire = format_message(msg)
    body = wire.split("```")[1]
    envelope = json.loads(body)
    assert envelope["version"] == ENVELOPE_VERSION
    assert envelope["kind"] == "result"
    assert envelope["status"] == "error"
    assert envelope["text"] == "Build failed"
    assert envelope["command"] == "build"
    assert envelope["args"] == ["--clean"]
    assert envelope["ts"] == "2026-06-20T12:00:00Z"
    assert envelope["correlation_id"] == "corr-9"


def test_format_envelope_keys_are_sorted() -> None:
    msg = ChatMessage(kind="status", status="info", text="x", ts="2026-06-20T12:00:00Z")
    body = format_message(msg).split("```")[1]
    envelope = json.loads(body)
    assert list(envelope.keys()) == sorted(envelope.keys())


def test_format_populates_missing_timestamp_from_clock(frozen_clock: str) -> None:
    msg = ChatMessage(kind="status", status="info", text="x")
    body = format_message(msg).split("```")[1]
    envelope = json.loads(body)
    assert envelope["ts"] == frozen_clock


def test_format_preserves_existing_timestamp(frozen_clock: str) -> None:
    explicit = "2020-01-01T00:00:00Z"
    msg = ChatMessage(kind="status", status="info", text="x", ts=explicit)
    body = format_message(msg).split("```")[1]
    envelope = json.loads(body)
    assert envelope["ts"] == explicit
    assert envelope["ts"] != frozen_clock


def test_format_validates_before_emitting() -> None:
    """An invalid message never produces wire text; it fails fast."""
    msg = ChatMessage(kind="status", status=None, text="x", ts="2026-06-20T12:00:00Z")
    with pytest.raises(ValueError):
        format_message(msg)


# --------------------------------------------------------------------------- #
# parse_message: trigger-prefix command detection.
# --------------------------------------------------------------------------- #


def test_parse_trigger_line_extracts_command_and_args() -> None:
    parsed = parse_message(f"{DEFAULT_TRIGGER_PREFIX} deploy prod --force")
    assert parsed.kind == "command"
    assert parsed.command == "deploy"
    assert parsed.args == ["prod", "--force"]
    assert parsed.text == "deploy prod --force"


def test_parse_trigger_line_single_command_no_args() -> None:
    parsed = parse_message("claude: status")
    assert parsed.command == "status"
    assert parsed.args == []


def test_parse_trigger_line_populates_timestamp(frozen_clock: str) -> None:
    parsed = parse_message("claude: ping")
    assert parsed.ts == frozen_clock


def test_parse_trigger_line_strips_surrounding_whitespace() -> None:
    parsed = parse_message("   claude:    deploy   prod   ")
    assert parsed.command == "deploy"
    assert parsed.args == ["prod"]


def test_parse_trigger_line_collapses_internal_whitespace() -> None:
    """split() collapses runs of spaces/tabs into single arg boundaries."""
    parsed = parse_message("claude: run\t a\t  b")
    assert parsed.command == "run"
    assert parsed.args == ["a", "b"]


def test_parse_custom_trigger_prefix() -> None:
    parsed = parse_message("bot-command: ship now", trigger_prefix="bot-command:")
    assert parsed.command == "ship"
    assert parsed.args == ["now"]


def test_parse_default_prefix_not_matched_with_custom_prefix() -> None:
    """With a custom prefix, a default-prefixed line is not a command."""
    with pytest.raises(ValueError):
        parse_message("claude: deploy", trigger_prefix="bot-command:")


def test_parse_trigger_prefix_without_command_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_message("claude:    ")
    assert "no command" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# parse_message: JSON envelope forms.
# --------------------------------------------------------------------------- #


def test_parse_fenced_envelope() -> None:
    envelope = json.dumps({"version": "1", "kind": "status", "status": "info", "text": "hi"})
    text = f"summary line\n```\n{envelope}\n```"
    parsed = parse_message(text)
    assert parsed.kind == "status"
    assert parsed.status == "info"
    assert parsed.text == "hi"


def test_parse_fenced_envelope_with_language_tag() -> None:
    envelope = json.dumps({"version": "1", "kind": "status", "status": "info", "text": "hi"})
    text = f"```json\n{envelope}\n```"
    parsed = parse_message(text)
    assert parsed.kind == "status"
    assert parsed.text == "hi"


def test_parse_bare_json_object() -> None:
    envelope = json.dumps({"version": "1", "kind": "result", "status": "success", "text": "ok"})
    parsed = parse_message(envelope)
    assert parsed.kind == "result"
    assert parsed.status == "success"


def test_parse_unclosed_fence_falls_through_to_error() -> None:
    """A single (unterminated) fence is not a valid envelope and is rejected."""
    with pytest.raises(ValueError):
        parse_message("``` not closed and not a trigger")


# --------------------------------------------------------------------------- #
# parse_message: error paths.
# --------------------------------------------------------------------------- #


def test_parse_none_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        # Deliberately exercise the None-guard via a typed escape hatch (no
        # suppression comment): cast keeps the call type-clean.
        parse_message(cast(str, None))
    assert "None" in str(exc_info.value)


def test_parse_plain_text_without_trigger_or_envelope_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_message("just chatting, no command here")
    assert "claude:" in str(exc_info.value)


def test_parse_malformed_json_envelope_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_message("```\n{not valid json}\n```")
    assert "JSON" in str(exc_info.value)


def test_parse_non_object_json_envelope_raises() -> None:
    """A fenced JSON array is valid JSON but not an envelope object."""
    with pytest.raises(ValueError) as exc_info:
        parse_message("```\n[1, 2, 3]\n```")
    assert "object" in str(exc_info.value)


def test_parse_bare_json_array_is_not_an_envelope() -> None:
    """A bare (unfenced) array does not start with '{', so it is plain text."""
    with pytest.raises(ValueError) as exc_info:
        parse_message("[1, 2, 3]")
    assert "claude:" in str(exc_info.value)


def test_parse_envelope_rejects_bad_version() -> None:
    envelope = json.dumps({"version": "2", "kind": "status", "status": "info", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "version" in str(exc_info.value).lower()


def test_parse_envelope_rejects_unknown_kind() -> None:
    envelope = json.dumps({"version": "1", "kind": "frobnicate", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "kind" in str(exc_info.value).lower()


def test_parse_envelope_rejects_unknown_status() -> None:
    envelope = json.dumps({"version": "1", "kind": "status", "status": "halfway", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "status" in str(exc_info.value).lower()


def test_parse_status_kind_requires_status() -> None:
    envelope = json.dumps({"version": "1", "kind": "status", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "status" in str(exc_info.value).lower()


def test_parse_result_kind_requires_status() -> None:
    envelope = json.dumps({"version": "1", "kind": "result", "text": "x"})
    with pytest.raises(ValueError):
        parse_message(envelope)


def test_parse_command_kind_requires_command() -> None:
    """An envelope of kind 'command' with no command field fails validation."""
    envelope = json.dumps({"version": "1", "kind": "command", "text": "x"})
    with pytest.raises(ValueError) as exc_info:
        parse_message(envelope)
    assert "command" in str(exc_info.value).lower()


# --------------------------------------------------------------------------- #
# Round-trips across all kinds (format then parse is identity on wire fields).
# --------------------------------------------------------------------------- #


def test_round_trip_status_kind() -> None:
    msg = ChatMessage(
        kind="status",
        status="success",
        text="Tests passed",
        ts="2026-06-20T12:00:00Z",
        correlation_id="abc-123",
    )
    parsed = parse_message(format_message(msg))
    assert parsed.kind == "status"
    assert parsed.status == "success"
    assert parsed.text == "Tests passed"
    assert parsed.ts == "2026-06-20T12:00:00Z"
    assert parsed.correlation_id == "abc-123"
    assert parsed.version == ENVELOPE_VERSION


def test_round_trip_result_kind() -> None:
    msg = ChatMessage(kind="result", status="error", text="Build failed", ts="2026-06-20T13:30:00Z")
    parsed = parse_message(format_message(msg))
    assert parsed.kind == "result"
    assert parsed.status == "error"
    assert parsed.text == "Build failed"


def test_round_trip_command_kind_preserves_args() -> None:
    msg = ChatMessage(
        kind="command",
        command="deploy",
        args=["prod", "--force"],
        text="deploy prod --force",
        ts="2026-06-20T12:00:00Z",
    )
    parsed = parse_message(format_message(msg))
    assert parsed.kind == "command"
    assert parsed.command == "deploy"
    assert parsed.args == ["prod", "--force"]


@pytest.mark.parametrize("status", sorted(ALLOWED_STATUSES))
def test_round_trip_every_status(status: str) -> None:
    msg = ChatMessage(kind="status", status=status, text="t", ts="2026-06-20T12:00:00Z")
    parsed = parse_message(format_message(msg))
    assert parsed.status == status
