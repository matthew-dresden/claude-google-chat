---
name: google-chat
description: Session-bound Google Chat ChatOps protocol for Claude Code — one shared space, a thread per session, name-prefix routing for new threads, a dispatcher menu for unrouted messages, and how to consume 'cgc listen --session' JSON events and reply in-thread. Use when connecting a session, routing/replying to a chat message, or formatting a ChatOps status.
---

# Google Chat ChatOps protocol (session-bound)

This skill documents the **session-bound, multi-instance** protocol used by the
`claude-google-chat` plugin and the `cgc` CLI so that several Claude Code
instances and humans can share **one Google Chat space** unambiguously. The CLI
is the source of truth; this skill describes the rules and the wire format. The
actual sending/reading is performed by `cgc`.

## The session model

- **One shared space.** Every connected session lives in the same Google Chat
  space. There is no per-session space.
- **A thread per session, but a session owns many threads.** Connecting a session
  (`cgc connect [NAME]`) opens its **primary thread**. The session then accrues
  additional threads as humans start new ones addressed to it.
- **A session is a named working context.** `NAME` is explicit or derived
  deterministically from the git repo basename + branch + a short hash of the
  current directory, so two checkouts get distinct, stable names and reconnect is
  idempotent.
- **One dispatcher.** The first session connected auto-becomes the **dispatcher**
  (or force it with `cgc connect --dispatcher`). The dispatcher answers
  *unrouted* new threads with a menu (see below). At most one session is the
  dispatcher at a time; disconnecting it promotes a survivor.

State lives in the durable session registry (`sessions_file`, default
`<config_dir>/sessions.json`, written `0600`, no secrets): session name → space,
claimed threads, dispatcher flag, `created_at`.

## Lifecycle commands

- `cgc connect [NAME] [--space SPACE] [--dispatcher]` — create/reuse a session and
  open its primary thread. Idempotent on `NAME`. Prints routing instructions
  (resolved name + primary thread + how to talk to it).
- `cgc session list` — show sessions, their threads, and which is the dispatcher.
- `cgc disconnect NAME [--notify]` — remove a session (promote a new dispatcher if
  needed); `--notify` posts a "disconnected" note to its primary thread first.
- `cgc listen --session NAME` — routing-aware listen for one session (see below).

## Routing rules (`cgc listen --session NAME`)

For each new message from a **HUMAN** sender in the shared space, with owning
thread `T` and text, the listener decides:

- **Reply in one of my threads** — `T` is a thread claimed by `NAME` →
  **EMIT** it as work (a reply in my thread).
- **New `NAME:` thread** — `T` is unclaimed and the text starts with `NAME:`
  (case-insensitive, optional space) → **CLAIM** `T` for `NAME` (persist it) and
  **EMIT**, with the `NAME:` prefix stripped from the surfaced text.
- **Dispatcher + truly-unrouted new thread** — `T` is unclaimed, the text starts
  with **no** registered session name, and `NAME` is the dispatcher → post the
  **"which session?" menu** into `T` (this is **not** emitted as work).
- **Thread owned by another session** — `T` is claimed by a different session →
  **skip** (nothing emitted).

Non-human senders (BOT/app/webhook) are never surfaced, so a session never echoes
its own outbound posts or another bot (loop prevention). Routing reuses the same
trigger / catch-all conversion, resilience, durable high-water resume, and
idle-timeout behavior as plain `listen`. The session must already exist
(`cgc connect NAME`) or `listen --session` fails fast.

### Dispatcher menu (unrouted messages)

When a human posts a top-level message that does not start with any registered
session name and lands in a fresh thread, the **dispatcher** automatically posts a
"which session should handle this?" menu listing the registered session names and
explaining how to route (reply in a thread, or start with `NAME: <message>`). The
human then re-addresses it. Claude does **not** act on these — they are not
emitted as work.

## Consuming `cgc listen --session` JSON events

The listener writes **one JSON line per emitted message** to stdout (12-factor
logs). Each line is the structured envelope below. To respond:

1. Parse the JSON line.
2. Read `text` (the human's request, with any `NAME:` prefix already stripped)
   and `thread_name` (the owning thread).
3. Do the requested work.
4. Reply **into the same thread** by passing `thread_name` as the thread key:

   ```bash
   cgc chat send --status <status> --text "<reply>" --thread-key "<thread_name>"
   ```

   Choose `--status` to reflect the outcome (`info` / `working` / `success` /
   `error` / `blocked`).

Example emitted event (a reply routed to session `myapp` in its primary thread):

```json
{
  "version": "1",
  "kind": "command",
  "status": null,
  "text": "run the tests",
  "command": "run",
  "args": ["the", "tests"],
  "ts": "2026-06-20T12:00:00Z",
  "correlation_id": null,
  "thread_name": "spaces/AAAA/threads/abcd1234",
  "session_name": "myapp"
}
```

## Structured message envelope

Every machine-readable message is a JSON object (the "envelope"), the single
source of truth produced/parsed by `cgc` (`messages.py`):

- **version** — Always the string `"1"`. Any other value is rejected (fail fast).
- **kind** — One of `"status"`, `"command"`, or `"result"`.
- **status** — Present for `status`/`result` kinds. One of `info`, `working`,
  `success`, `error`, `blocked`.
- **text** — Human-readable message body.
- **command** — Present for the `command` kind: the command name.
- **args** — Array of string arguments (may be empty).
- **ts** — RFC3339 UTC timestamp, e.g. `2026-06-20T12:00:00Z`.
- **correlation_id** — Optional id linking a `result` back to its `command`.
- **thread_name** — The owning Chat thread resource name
  (`spaces/.../threads/...`), surfaced on inbound events so you know which thread
  to reply into; `null` when unthreaded.
- **session_name** — The session a routed event was delivered to by
  `cgc listen --session NAME`; `null` for plain (non-session) listening.

## Clean human replies (no envelope by default)

By default a message posted to Google Chat is just the clean, emoji-prefixed
**summary line**:

```
✅ Tests passed
```

The machine-readable JSON envelope is **not** embedded in the human Chat view. The
machine channel is the JSONL emitted on `cgc listen` stdout. To additionally embed
the envelope in the Chat text, opt in per send with `cgc chat send --envelope`
(or set `send_envelope = true` / `CGC_SEND_ENVELOPE=true`), which appends a fenced
code block containing the JSON envelope after the summary line. Keep replies to
humans clean — use `--envelope` only when a tool on the other side needs to parse
the message.

## Status → emoji mapping

The single source of truth shared by formatting and validation:

- **info** — ℹ️
- **working** — ⏳
- **success** — ✅
- **error** — ❌
- **blocked** — ⛔

## Inbound text: trigger prefix and catch-all

How inbound text is converted to an event depends on `require_trigger`:

- **`require_trigger = true` (default)** — only text starting with the configured
  trigger prefix (default `claude:`) is surfaced, parsed as a structured command
  (`claude: <command> [args...]`). For example `claude: deploy prod --force`
  parses to `kind = command`, `command = deploy`, `args = ["prod", "--force"]`.
- **`require_trigger = false` (catch-all)** — every HUMAN message is surfaced; a
  trigger-prefixed line still parses as a command, a plain conversational line is
  surfaced carrying its full text.

The trigger prefix is configurable via `CGC_TRIGGER_PREFIX`. In the session-bound
flow, the `NAME:` routing prefix is handled by the router *before* this
conversion and is stripped from the surfaced `text`.

## Operational rules

These rules are enforced throughout the plugin and CLI and MUST be honored:

- **Never log secrets** — webhook tokens, OAuth tokens, and client secrets are
  never printed or echoed. `cgc config show` masks them; `sessions_file` holds no
  secrets.
- **Fail fast** — invalid input, missing required configuration, an unknown
  session, or a non-2xx HTTP response causes a non-zero exit with a clear,
  actionable message. There are no silent fallbacks.
- **No `sleep`-based waiting** — the listener polls on an env-driven cadence
  (`CGC_POLL_INTERVAL`) and enforces an env-driven idle timeout
  (`CGC_LISTEN_TIMEOUT`) that exits non-zero on expiry. Time delays are never used
  as a synchronization or readiness primitive.
- **Logs to stdout** — emitted messages are written unbuffered to stdout as JSON
  lines, following 12-factor logging.
