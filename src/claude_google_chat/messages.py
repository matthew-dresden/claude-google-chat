"""Structured ChatOps message format.

This module is pure (no I/O) so it can be unit-tested in isolation and acts as
the single source of truth for the message envelope, allowed values, and the
status-to-emoji mapping. Both validation and the test-suite consume the
module-level constants below, keeping the format DRY.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

ENVELOPE_VERSION = "1"

# Single source of truth for the allowed value sets. Both validation in
# parse_message and the test-suite import these constants.
ALLOWED_KINDS: frozenset[str] = frozenset({"status", "command", "result"})
ALLOWED_STATUSES: frozenset[str] = frozenset({"info", "working", "success", "error", "blocked"})

# Status -> emoji mapping (DRY: defined once, used by format_message and tests).
STATUS_EMOJI: dict[str, str] = {
    "info": "ℹ️",  # information source
    "working": "⏳",  # hourglass
    "success": "✅",  # check mark
    "error": "❌",  # cross mark
    "blocked": "⛔",  # no entry
}

DEFAULT_TRIGGER_PREFIX = "claude-command:"


@dataclass(frozen=True)
class ChatMessage:
    """A structured ChatOps message exchanged via Google Chat.

    Attributes:
        kind: One of :data:`ALLOWED_KINDS`.
        text: Human-readable summary line.
        status: For ``status``/``result`` kinds, one of :data:`ALLOWED_STATUSES`.
        command: For ``command`` kind, the command name.
        args: Positional arguments (for ``command`` kind).
        ts: RFC3339 UTC timestamp.
        correlation_id: Optional id to correlate request/response messages.
        version: Envelope version (always ``"1"``).
    """

    kind: str
    text: str = ""
    status: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    ts: str | None = None
    correlation_id: str | None = None
    version: str = ENVELOPE_VERSION


def _now_rfc3339() -> str:
    """Return the current UTC time as an RFC3339 string with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(msg: ChatMessage) -> None:
    """Validate a :class:`ChatMessage`, raising ``ValueError`` on any problem.

    Fails fast with an actionable message; there is no silent fallback.
    """
    if msg.version != ENVELOPE_VERSION:
        raise ValueError(
            f"unsupported envelope version {msg.version!r}; expected version {ENVELOPE_VERSION!r}"
        )
    if msg.kind not in ALLOWED_KINDS:
        raise ValueError(f"unknown kind {msg.kind!r}; allowed kinds are {sorted(ALLOWED_KINDS)}")
    if msg.status is not None and msg.status not in ALLOWED_STATUSES:
        raise ValueError(
            f"unknown status {msg.status!r}; allowed statuses are {sorted(ALLOWED_STATUSES)}"
        )
    if msg.kind in ("status", "result") and msg.status is None:
        raise ValueError(f"kind {msg.kind!r} requires a status field")
    if msg.kind == "command" and not msg.command:
        raise ValueError("kind 'command' requires a command field")


def _envelope_dict(msg: ChatMessage) -> dict[str, object]:
    """Build the JSON-serialisable envelope dict for a message."""
    return {
        "version": msg.version,
        "kind": msg.kind,
        "status": msg.status,
        "text": msg.text,
        "command": msg.command,
        "args": list(msg.args),
        "ts": msg.ts,
        "correlation_id": msg.correlation_id,
    }


def format_message(msg: ChatMessage) -> str:
    """Produce the on-the-wire Google Chat text for a message.

    The wire form is a human-readable summary line followed by a fenced code
    block containing the JSON envelope. A timestamp is populated if absent.
    """
    populated = msg if msg.ts else replace(msg, ts=_now_rfc3339())
    _validate(populated)

    emoji = STATUS_EMOJI[populated.status] if populated.status else ""
    summary = f"{emoji} {populated.text}".strip()
    envelope = json.dumps(_envelope_dict(populated), indent=2, sort_keys=True)
    return f"{summary}\n```\n{envelope}\n```"


def to_jsonl(msg: ChatMessage) -> str:
    """Serialise a message to a single canonical JSON line (no trailing newline).

    The single source of truth for the stdout/log JSON shape of a
    :class:`ChatMessage`, built from the same :func:`_envelope_dict` envelope as
    :func:`format_message` so the on-the-wire and log representations never drift
    as dataclass fields change. Used by the ``listen`` and ``serve`` loops.
    """
    return json.dumps(_envelope_dict(msg), sort_keys=True)


def _extract_json_block(text: str) -> str | None:
    """Return the contents of the first fenced code block, or ``None``."""
    start = text.find("```")
    if start == -1:
        return None
    end = text.find("```", start + 3)
    if end == -1:
        return None
    block = text[start + 3 : end]
    # Drop an optional language tag on the opening fence line.
    if "\n" in block:
        first_line, rest = block.split("\n", 1)
        if first_line.strip() and not first_line.strip().startswith("{"):
            block = rest
    return block.strip()


def _parse_envelope(raw: str) -> ChatMessage:
    """Parse a JSON envelope string into a validated :class:`ChatMessage`."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON envelope: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("envelope must be a JSON object")

    msg = ChatMessage(
        kind=str(data.get("kind", "")),
        text=str(data.get("text", "")),
        status=data.get("status"),
        command=data.get("command"),
        args=list(data.get("args", [])),
        ts=data.get("ts"),
        correlation_id=data.get("correlation_id"),
        version=str(data.get("version", "")),
    )
    _validate(msg)
    return msg


def parse_message(text: str, trigger_prefix: str = DEFAULT_TRIGGER_PREFIX) -> ChatMessage:
    """Parse inbound Google Chat text into a :class:`ChatMessage`.

    Accepts either a fenced JSON envelope or a trigger-prefixed plain line of
    the form ``<prefix> <command> [args...]``. Raises ``ValueError`` with a
    clear message on invalid input (fail fast, no silent fallback).
    """
    if text is None:
        raise ValueError("cannot parse message from None")

    stripped = text.strip()

    if stripped.startswith(trigger_prefix):
        remainder = stripped[len(trigger_prefix) :].strip()
        if not remainder:
            raise ValueError(
                f"trigger line {stripped!r} contains no command after prefix {trigger_prefix!r}"
            )
        parts = remainder.split()
        command, args = parts[0], parts[1:]
        return ChatMessage(
            kind="command",
            text=remainder,
            command=command,
            args=args,
            ts=_now_rfc3339(),
        )

    block = _extract_json_block(text)
    if block is not None:
        return _parse_envelope(block)

    # A bare JSON object is also acceptable.
    if stripped.startswith("{"):
        return _parse_envelope(stripped)

    raise ValueError(
        "message is neither a trigger-prefixed command "
        f"(expected prefix {trigger_prefix!r}) nor a fenced JSON envelope"
    )
