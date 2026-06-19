# Usage

This page covers the `cgc` CLI, the Claude Code plugin commands, the listener behavior, and the structured message format used in both directions.

---

## Command reference

### `cgc config`

| Command | Purpose |
|---|---|
| `cgc config init` | Create the user config file in the OS config dir if it does not exist. |
| `cgc config show` | Print the effective config (file + env merged). Secrets are masked. |
| `cgc config set <key> <value>` | Write a value to the user config file. |

```bash
cgc config init
cgc config set webhook_url "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
cgc config set space_id "spaces/AAAA"
cgc config set trigger_prefix "claude-command:"
cgc config show
```

`cgc config show` never prints the full webhook token or the cached token-file contents.

### `cgc auth`

| Command | Purpose |
|---|---|
| `cgc auth login` | Run the installed-app OAuth flow and cache the user token. |

```bash
cgc auth login
```

Required only for reading inbound messages (`cgc listen`). Outbound sends use the webhook and need no OAuth. Fails fast if the OAuth client secrets file is missing.

### `cgc chat send`

Send a structured status ping to the configured space via the incoming webhook.

```bash
cgc chat send --status success --text "Build is green"
cgc chat send --status working --text "Running tests"
cgc chat send --status error   --text "Migration failed on step 3"
```

`--status` is one of `info | working | success | error | blocked`. A non-2xx HTTP response causes a fail-fast non-zero exit with the status code and a redacted URL.

### `cgc listen`

Start the inbound listener.

```bash
cgc listen                  # run forever; idle timeout from CGC_LISTEN_TIMEOUT (0 = forever)
cgc listen --once           # drain currently-pending messages and exit (for hooks/CI)
cgc listen --timeout 300    # exit non-zero if idle for 300 seconds
```

Each new message is emitted as a single JSON line to stdout. Only messages whose text starts with the configured trigger prefix (default `claude-command:`) are surfaced as commands.

---

## Plugin commands (inside Claude Code)

### `/claude-google-chat:chat-setup`

Interactive one-time setup. It:

1. Verifies `cgc` is on your `PATH` (and prints install instructions if not).
2. Shows current config and which env vars / config keys are required.
3. Walks you through `CGC_WEBHOOK_URL`, `CGC_SPACE_ID`, the OAuth client file path, and `CGC_TRIGGER_PREFIX`, writing them via `cgc config set`.
4. Runs `cgc auth login` if inbound reading is desired.
5. Verifies with a test send and confirms a 200.

```
/claude-google-chat:chat-setup
/claude-google-chat:chat-setup --space spaces/AAAA
```

### `/claude-google-chat:chat-send`

Send a status ping. The first token is the status; the rest is the text.

```
/claude-google-chat:chat-send success Build is green
/claude-google-chat:chat-send working Running the integration suite
```

This runs `cgc chat send --status "<status>" --text "<text>"` and surfaces the HTTP result.

### `/claude-google-chat:chat-listener`

Start the listener and surface inbound commands.

```
/claude-google-chat:chat-listener
/claude-google-chat:chat-listener --once
/claude-google-chat:chat-listener --timeout 600
```

This runs `cgc listen <args>`. Each emitted line is a structured JSON message. `--once` drains pending messages and exits (useful in a `Stop` hook or in CI). Timeouts are env-driven (`CGC_LISTEN_TIMEOUT`) and never `sleep`-based.

---

## Listener behavior

- The listener polls the space on a **documented, env-driven cadence** (`CGC_POLL_INTERVAL`, default `2.0` seconds). The poll interval is a deliberate cadence, not a `sleep`-based readiness wait.
- It tracks the last seen message id and yields only newer messages.
- It filters to messages whose text starts with the trigger prefix.
- An **idle timeout** (`CGC_LISTEN_TIMEOUT`, default `0` = run forever) causes a **fail-fast non-zero exit** with a clear diagnostic when no qualifying message arrives within the window.
- `--once` drains currently-pending messages and returns, so it composes cleanly with hooks and CI.
- Output is unbuffered JSON lines on stdout (12-factor logs).

---

## Structured message format

Both directions use one envelope (defined in `messages.py`). The JSON object:

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

Fields:

- `version` — always `"1"`.
- `kind` — one of `status | command | result`.
- `status` — for `status` and `result` kinds, one of `info | working | success | error | blocked`.
- `text` — human-readable summary.
- `command` — for `command` kind, the command name.
- `args` — array of string arguments.
- `ts` — RFC3339 UTC timestamp.
- `correlation_id` — optional, links a result back to a command.

### On-the-wire (outbound)

`format_message` produces a single Google Chat message: a human-readable summary line (with a status emoji) followed by the JSON envelope in a fenced code block.

```
✅ Build is green
```json
{"version":"1","kind":"status","status":"success","text":"Build is green","command":null,"args":[],"ts":"2026-06-19T12:00:00Z","correlation_id":null}
```
```

Status → emoji mapping (the single source of truth in `messages.py`):

| Status | Emoji |
|---|---|
| `info` | ℹ️ |
| `working` | ⏳ |
| `success` | ✅ |
| `error` | ❌ |
| `blocked` | ⛔ |

### Inbound trigger form

Humans send commands as a plain line starting with the trigger prefix:

```
claude-command: deploy prod --force
```

`parse_message` reads this as `kind == "command"`, `command == "deploy"`, `args == ["prod", "--force"]`. The trigger prefix is configurable via `CGC_TRIGGER_PREFIX`.

`parse_message` accepts either a fenced JSON envelope or a trigger-prefixed plain line, validates `version`, `kind`, and `status`, and raises `ValueError` with a clear message on invalid input — it never silently falls back.

---

## Examples by kind

**Status** (Claude → space):

```
⏳ Running tests
```json
{"version":"1","kind":"status","status":"working","text":"Running tests","command":null,"args":[],"ts":"2026-06-19T12:00:00Z","correlation_id":null}
```
```

**Command** (human → space):

```
claude-command: rerun-ci --branch main
```

**Result** (Claude → space, linked to a command):

```
✅ CI rerun complete
```json
{"version":"1","kind":"result","status":"success","text":"CI rerun complete","command":null,"args":[],"ts":"2026-06-19T12:05:00Z","correlation_id":"abc123"}
```
```

See [configuration.md](configuration.md) for tuning the trigger prefix, poll interval, and timeouts.
