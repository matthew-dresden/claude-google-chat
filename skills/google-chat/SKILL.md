---
name: google-chat
description: Google Chat ChatOps protocol for Claude Code — the structured message format and how to send status pings, read inbound commands, and recognize the claude: trigger. Use when sending status to Google Chat, interpreting a chat command, or formatting a ChatOps message.
---

# Google Chat ChatOps protocol

This skill documents the structured message protocol used by the
`claude-google-chat` plugin and the `cgc` CLI so that Claude Code and humans can
exchange **status**, **commands**, and **results** over a Google Chat space
unambiguously. It is informational — it describes the wire format and rules; the
actual sending/reading is performed by the `cgc` CLI.

## Structured message envelope

Every machine-readable message is a JSON object (the "envelope"). The envelope is
the single source of truth and is produced/parsed by `cgc` (`messages.py`).

- **version** — Always the string `"1"`. Any other value is rejected (fail fast).
- **kind** — One of `"status"`, `"command"`, or `"result"`.
- **status** — Present for `status` and `result` kinds. One of `info`, `working`, `success`, `error`, `blocked`.
- **text** — Human-readable message body.
- **command** — Present for the `command` kind: the command name (e.g. `deploy`).
- **args** — Array of string arguments (may be empty).
- **ts** — RFC3339 UTC timestamp, e.g. `2026-06-19T12:00:00Z`.
- **correlation_id** — Optional id linking a `result` back to its `command`.

Example envelope:

```json
{
  "version": "1",
  "kind": "status",
  "status": "working",
  "text": "Running tests",
  "command": null,
  "args": [],
  "ts": "2026-06-19T12:00:00Z",
  "correlation_id": null
}
```

## On-the-wire representation

By default a message posted to Google Chat is just the clean, human-readable
summary line (prefixed with a status emoji):

```
⏳ Running tests
```

The machine-readable JSON envelope is **not** embedded in the human Chat view by
default. The machine channel is the JSONL emitted on `cgc listen`
stdout (one envelope per line). To additionally embed the envelope in the Chat
text, opt in with `send_envelope = true` (`CGC_SEND_ENVELOPE=true`) or
`cgc chat send --envelope`, which yields the summary line followed by a fenced
code block containing the JSON envelope:

```
⏳ Running tests
```` (fenced) ````
{ ...json envelope... }
```` (fenced) ````
```

`cgc chat send` produces the summary line via `format_message`; with the envelope
enabled the fenced JSON is appended so tools can parse it exactly. `parse_message`
accepts both the trigger-prefixed line and the fenced/bare JSON envelope.

## Status → emoji mapping

The mapping is a single source of truth shared by formatting and validation:

- **info** — ℹ️
- **working** — ⏳
- **success** — ✅
- **error** — ❌
- **blocked** — ⛔

## Inbound commands and the trigger prefix

Inbound commands are recognized when a Google Chat message text **starts with the
configured trigger prefix**, followed by the command and its arguments:

```
claude: <command> [args...]
```

For example, `claude: deploy prod --force` parses to:

- **kind**: `command`
- **command**: `deploy`
- **args**: `["prod", "--force"]`

The trigger prefix is configurable via `CGC_TRIGGER_PREFIX` (default
`claude:`). The listener filters the space to messages whose text starts
with this prefix and emits each as a parsed envelope (a JSON line on stdout).

## Examples by kind

**status** — Claude reporting progress outbound:

```json
{ "version": "1", "kind": "status", "status": "working", "text": "Building image", "command": null, "args": [], "ts": "2026-06-19T12:00:00Z", "correlation_id": null }
```

**command** — a human asking Claude to act (inbound, trigger form):

```
claude: run-tests unit --fast
```

**result** — Claude reporting the outcome, linked by `correlation_id`:

```json
{ "version": "1", "kind": "result", "status": "success", "text": "All tests passed", "command": null, "args": [], "ts": "2026-06-19T12:03:00Z", "correlation_id": "run-tests-001" }
```

## How `cgc` parses and produces messages

- `format_message(msg)` renders the summary line + fenced JSON wire form.
- `parse_message(text)` accepts **either** a fenced JSON envelope **or** a
  trigger-prefixed plain line, validates it, and returns a `ChatMessage`.
- `parse_message` raises `ValueError` with a clear message when `version != "1"`,
  `kind` is unknown, or `status` is not in the allowed set — no silent fallback.
- `status`/`result` envelopes round-trip exactly through
  `format_message` → `parse_message`.

## Operational rules

These rules are enforced throughout the plugin and CLI and MUST be honored when
producing or interpreting messages:

- **Never log secrets** — webhook tokens, OAuth tokens, and client secrets are never
  printed or echoed. `cgc config show` masks them.

- **Fail fast** — invalid input, missing required configuration, or a non-2xx HTTP
  response causes a non-zero exit with a clear, actionable message. There are no
  silent fallbacks.

- **No `sleep`-based waiting** — the listener polls on an env-driven cadence
  (`CGC_POLL_INTERVAL`) and enforces an env-driven idle timeout
  (`CGC_LISTEN_TIMEOUT`) that exits non-zero on expiry. Time delays are never used
  as a synchronization or readiness primitive.

- **Logs to stdout** — emitted messages are written unbuffered to stdout as JSON
  lines, following 12-factor logging.
