---
description: Connect this Claude Code instance as a named Google Chat session, then listen for and reply to messages routed to it. Bind one shared space, thread-per-session.
argument-hint: "[name]"
allowed-tools: Bash
disable-model-invocation: true
---

# Connect a Google Chat session and start listening

Bind this Claude Code instance to a named **session** in the shared Google Chat
space, open its primary thread, and then listen for messages routed to it —
replying in-thread as you do the requested work. This is the session-bound,
multi-instance ChatOps flow: one shared space, a thread per session, with
name-prefix routing for new threads and a dispatcher menu for unrouted messages.

Optional session NAME passed to this command: `$ARGUMENTS` (when empty, the CLI
derives a stable name from the git repo + branch + a short hash of the current
directory).

For the full session-bound protocol — routing rules, the dispatcher menu, clean
human replies, and the shape of `cgc listen --session` JSON events — see the
`/claude-google-chat:google-chat` skill.

Follow these steps in order. **Stop and report** the moment any step fails — do
not continue past a failure, and do not invent fallback behavior.

## 0. Verify the `cgc` CLI is installed

```bash
cgc --version
```

If the command is not found, `cgc` is missing from your `PATH`. Tell the user to
install it and run setup, then **stop**:

```bash
pipx install claude-google-chat   # or: uv tool install claude-google-chat / pip install claude-google-chat
```

Then run `/claude-google-chat:setup`.

## 1. Diagnose prerequisites (`cgc doctor`)

```bash
cgc doctor
```

`cgc doctor` prints a RED/GREEN (`[PASS]`/`[FAIL]`) checklist with the **exact
fix command** per red line (gcloud, OAuth/ADC credentials and scopes, webhook,
space, config file). It exits non-zero when any required check fails.

If anything is red, **stop** and surface the failing line(s) and the printed fix
to the user (typically `/claude-google-chat:setup`). Do not proceed to connect.

## 2. Connect the session (`cgc connect`)

Register the session and create (or reuse) its primary thread:

```bash
cgc connect $ARGUMENTS
```

- Pass the optional NAME from `$ARGUMENTS`; omit it to let the CLI derive a
  stable name from git repo + branch + cwd.
- `connect` is **idempotent**: reconnecting an existing NAME reuses its registry
  entry and primary thread (it does not post a duplicate opening message).
- The **first** session connected auto-becomes the **dispatcher**; pass
  `--dispatcher` only to force a specific session into that role.
- It requires `webhook_url` and a resolvable space; on failure it exits non-zero
  with an actionable message — surface it and **stop**.

`cgc connect` prints routing instructions including the resolved session NAME and
its primary thread. **Capture the resolved NAME** from that output — you need it
for the next step. Run `cgc session list` if you need to confirm the name,
threads, and which session is the dispatcher.

## 3. Listen for this session and reply in-thread

Arm the routing-aware listener bound to the resolved session NAME:

```bash
cgc listen --session <name>
```

This emits **one JSON line per message routed to this session** (a reply in one
of its claimed threads, or a new `name: ...` thread it claims). Each event is the
structured envelope and carries `text`, `thread_name` (the owning thread), and
`session_name`.

For **each** emitted message event:

1. Parse the JSON line; read `text` (the human's request) and `thread_name`.
2. Do the requested work.
3. Reply by posting back into **that same thread**:

   ```bash
   cgc chat send --status <status> --text "<your reply>" --thread-key <thread_name>
   ```

   Use the event's `thread_name` as the `--thread-key` so the reply lands in the
   thread the human is talking to. Pick a `--status` that reflects the outcome
   (`info` / `working` / `success` / `error` / `blocked`). Keep replies clean and
   human-readable (no JSON envelope unless the user asks for `--envelope`).

The listener polls on an env-driven cadence and fails fast (non-zero exit) on its
idle timeout (`CGC_LISTEN_TIMEOUT`) — never a silent stop. To stop it, interrupt
the foreground process (Ctrl-C). Surface any non-zero exit and its diagnostic; do
not retry silently.

**Dispatcher behavior.** If this session is the dispatcher, the listener does
**not** emit truly-unrouted new threads (a top-level message that does not start
with any registered session name) as work — it automatically posts a "which
session?" menu into that thread so the human can re-address it. You only act on
events that are actually emitted.

## 4. Tell the user how to talk to this session

Report to the user:

- The **session NAME** it is now listening as.
- **To talk to it:** reply inside its thread (the primary thread `cgc connect`
  opened, or any thread it has claimed).
- **To open a new thread for it:** post a top-level message
  `<name>: <your message>` in the shared space — the listener claims that thread
  for this session and surfaces the message (with the `name:` prefix stripped).
- If this session is the **dispatcher**, an unaddressed top-level message gets the
  automatic "which session?" menu.

To stop routing to this session later, run `/claude-google-chat:disconnect
<name>`.
