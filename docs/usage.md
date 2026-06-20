# Usage

This page covers the `cgc` CLI, the Claude Code plugin commands, the listener behavior, and the structured message format used in both directions.

---

## Command reference

### `cgc config`

| Command | Purpose |
|---|---|
| `cgc config init` | Create the user config file in the OS config dir if it does not exist. |
| `cgc config show` | Print the effective config (file + env merged). Secrets are masked. |
| `cgc config get <key>` | Print one resolved value (secrets masked); exits non-zero on an unknown key. |
| `cgc config set <key> <value>` | Write a value to the user config file. |

```bash
cgc config init
cgc config set webhook_url "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
cgc config set space_id "spaces/AAAA"
cgc config set trigger_prefix "claude:"
cgc config get space_id
cgc config show
```

Tab completion suggests known config keys for `cgc config get <key>` and `cgc config set <key>` (see [Shell completion](#shell-completion)).

`cgc config show` never prints the full webhook token or the cached token-file contents.

### `cgc auth`

| Command | Purpose |
|---|---|
| `cgc auth login` | Run the installed-app OAuth flow and cache the user token. |

```bash
cgc auth login
```

Required only for reading inbound messages (`cgc listen`). Outbound sends use the webhook and need no OAuth. Fails fast if the OAuth client secrets file is missing.

`--client-file <path>` overrides the configured `oauth_client_file` for a single run; the path is validated to exist and be readable before the flow starts.

### `cgc chat send`

Send a structured status ping to the configured space via the incoming webhook.

```bash
cgc chat send --status success --text "Build is green"
cgc chat send --status working --text "Running tests"
cgc chat send --status error   --text "Migration failed on step 3"
```

`--status` is one of `info | working | success | error | blocked`. A non-2xx HTTP response causes a fail-fast non-zero exit with the status code and a redacted URL.

**Clean by default.** The message posted to Chat is just the emoji-prefixed summary line (e.g. `✅ Build is green`) — the machine-readable JSON envelope is **not** embedded in the human-facing Chat view. The machine channel is the JSONL on `cgc listen` stdout. To embed the JSON envelope in the Chat text, set `send_envelope = true` (`CGC_SEND_ENVELOPE=true`) in config, or override per send:

```bash
cgc chat send --status success --text "Build is green" --envelope     # append the JSON envelope
cgc chat send --status success --text "Build is green" --no-envelope   # force clean summary only
```

`--envelope` / `--no-envelope` is tri-state: omit it to use the resolved `send_envelope` config value (default `false`); pass either flag to override config for that one send.

**Post into a thread (`--thread-key`).** Pass `--thread-key <KEY>` to route the message into a caller-keyed thread: repeated sends with the **same** key land in the same thread, an **unseen** key starts a new one. The created message's stable `thread.name` is printed to **stderr** so you can capture it for read-filtering; stdout stays the `sent` line.

```bash
cgc chat send --status working --text "Deploying" --thread-key deploy-42
# stderr: thread: spaces/AAAA/threads/abcd1234
# stdout: sent
```

Capture the printed thread name to later read only that thread with `cgc listen --thread <THREAD_NAME>`. With no `--thread-key`, sends are unthreaded (unchanged behaviour).

### `cgc listen`

Start the inbound listener.

```bash
cgc listen                          # run forever; idle timeout from CGC_LISTEN_TIMEOUT (0 = forever)
cgc listen --once                   # drain currently-pending messages and exit (for hooks/CI)
cgc listen --timeout 300            # exit non-zero if idle for 300 seconds
cgc listen --space-id spaces/AAAA   # override the configured space for this run
cgc listen --thread spaces/AAAA/threads/T1 --thread spaces/AAAA/threads/T2  # only these threads
```

Each new message is emitted as a single JSON line to stdout. By default (`require_trigger = true`) only messages whose text starts with the configured trigger prefix (default `claude:`) are surfaced as commands. `--space-id` overrides the configured `space_id` for one run; required keys are still checked and fail fast when missing. Each emitted JSON event carries a `thread_name` field naming the thread the message belongs to (`null` when the message is unthreaded).

**Filter by thread (`--thread` / `threads`).** Pass one or more `--thread <THREAD_NAME>` flags (or set `threads` in config / `CGC_THREADS` as a comma list) to emit **only** messages whose `thread.name` is in that set. The thread filter composes *in addition to* the trigger and sender-type rules; with no threads configured the whole space is surfaced (unchanged). This pairs with `cgc chat send --thread-key` (capture the printed `thread.name`, then listen on it) to scope a two-way conversation to a single thread.

**Catch-all mode (`require_trigger = false`).** Set `require_trigger = false` (`CGC_REQUIRE_TRIGGER=false`) to surface **every** message from a HUMAN sender, not just trigger-prefixed ones. Trigger-prefixed lines still parse as structured commands; plain conversational lines are surfaced as a message carrying the full text. Non-human senders (BOT/app/webhook) are always excluded, so the listener never echoes its own outbound posts or other bots (loop prevention).

**Resilience.** A transient backend hiccup — a socket/connection timeout, a dropped connection, or a Chat API `408`/`429`/`5xx` — is logged to stderr as a concise, secret-free diagnostic and the loop continues on the normal cadence rather than crashing. A fatal auth/permission error (`401`/`403`) still fails fast immediately with an actionable message. After `max_consecutive_errors` (`CGC_MAX_CONSECUTIVE_ERRORS`, default `10`) **consecutive** transient failures the loop fails fast with a non-zero exit so a truly-down backend surfaces; the counter resets on any successful poll.

**Durable resume.** The poll high-water marker is persisted to `state_file` (`CGC_STATE_FILE`, default `<config_dir>/listen-state.json`, written `0600`). On restart the listener resumes from the last-processed message instead of re-reading recent history and re-emitting already-seen messages. A missing or corrupt state file degrades to a fresh start, never a crash.

### Sessions: `cgc connect` / `cgc session list` / `cgc disconnect`

The **session layer** binds a named working context (typically a git repo + branch + a Claude Code instance) to one or more Chat threads in the shared space, on top of the thread primitives above. State lives in `sessions_file` (`CGC_SESSIONS_FILE`, default `<config_dir>/sessions.json`, `0600`).

```bash
cgc connect                      # derive NAME from git repo+branch+cwd; open its primary thread
cgc connect myapp                # explicit NAME
cgc connect myapp --space spaces/AAAA   # override the shared space
cgc connect myapp --dispatcher   # force this session to be the dispatcher

cgc session list                 # show sessions, their threads, and which is the dispatcher
cgc disconnect myapp             # remove from the registry (re-elect a dispatcher if needed)
cgc disconnect myapp --notify    # also post a 'disconnected' note to its primary thread
```

`connect` opens (or reuses) the session's **primary thread** by sending an opening message keyed by the session name and recording the returned `thread.name`; it then prints routing instructions (how to reply, and how to start a new thread with a top-level `NAME: ...` message). Reconnecting an existing `NAME` is idempotent. The **first** session connected auto-becomes the **dispatcher**; `--dispatcher` forces it (only one session is the dispatcher at a time).

**Routing-aware listen (`cgc listen --session NAME`).** Run the listener bound to a session so inbound messages are routed:

```bash
cgc listen --session myapp
```

For each new HUMAN message with thread `T` and text:
- `T` is one of `myapp`'s claimed threads → **emitted** (a reply in my thread);
- `T` is unclaimed and the text starts with `myapp:` → `T` is **claimed** for `myapp` and emitted, with the `myapp:` prefix stripped from the surfaced text;
- `T` is unclaimed, `myapp` is the dispatcher, and the text starts with **no** registered session name → the dispatcher posts a "which session?" menu to `T` (not emitted as work);
- `T` is claimed by a **different** session → skipped.

Each emitted JSON event carries both `session_name` and `thread_name`. Routing reuses the same trigger / catch-all rules, resilience, and durable high-water resume as plain `listen`. The session must already exist (`cgc connect NAME`) or `listen --session` fails fast.

A typical multi-agent setup: run `cgc connect` in each repo/branch (the first becomes the dispatcher), then `cgc listen --session <name>` per agent. From your phone you reply inside a session's thread to talk to it, or start a top-level `name: <message>` to open a new thread for it; an unaddressed top-level message gets the dispatcher's menu.

### `cgc setup`

Guided, **idempotent, resumable** onboarding wizard — one command takes a fresh machine to a working two-way integration. Each step is verified before moving on, and re-running fixes only the gaps.

```bash
cgc setup              # full guided onboarding
cgc setup --reauth     # only redo authentication
cgc setup --dry-run    # show the actions that would run, change nothing
cgc setup --verify     # only run the end-to-end send/read round-trip check
```

Steps: detect gcloud (or print install link + console deep-links for manual setup) → create/select a project → enable the Chat API and **poll until ENABLED** (active readiness, not `sleep`; timeout from `CGC_SETUP_ENABLE_TIMEOUT`) → authenticate **ADC-first** (`gcloud auth application-default login`, no OAuth client to create) with a **guided OAuth-client fallback**, then verify the token carries every required Chat scope (re-auth on scope-drop) → prompt for and validate the webhook URL (token never echoed) → **verify end-to-end** with a real send + read-back round trip. On any failure it prints a concise, actionable error (no traceback or token) naming which step to re-run.

### `cgc doctor`

Print a RED/GREEN (`[PASS]`/`[FAIL]`) checklist of every prerequisite, with the **exact fix command** for each red line. Exits non-zero if any required check fails, so it doubles as a health gate.

```bash
cgc doctor
```

Checks: gcloud installed / logged in / project selected / Chat API enabled / OAuth-ADC credentials present & valid / token carries the required Chat scope / `webhook_url` configured & well-formed (token never echoed) / `space_id` configured / config file present. The config file path is printed in the footer.

### `cgc status`

Report which configuration values are present (secrets masked) and whether the
send and read paths are ready.

```bash
cgc status
```

### `cgc clear`

Delete trigger-prefixed messages from the configured space.

```bash
cgc clear                           # delete messages starting with the configured prefix
cgc clear --trigger-prefix "ops:"   # override the prefix for this run
```

### `cgc completion`

Print or install the tab-completion script for your shell. See [Shell completion](#shell-completion).

```bash
cgc completion bash                 # print the bash completion script to stdout
cgc completion zsh --install        # append the completion line to ~/.zshrc
```

---

## Shell completion

> For the full bash/zsh guide — auto-updating vs static-file installs, the `bash-completion` prerequisite, and verification — see the [Shell completion guide](SHELL_COMPLETION.md).

`cgc` ships full tab completion. There are two ways to enable it:

1. **Typer-native flags** (auto-detect the current shell):

   ```bash
   cgc --install-completion         # install for the detected shell
   cgc --show-completion            # print the script to copy/customize
   ```

2. **The friendlier `cgc completion` command** (explicit shell, idempotent install):

   ```bash
   cgc completion bash              # print the bash script
   cgc completion zsh --install     # append an eval line to ~/.zshrc (idempotent)
   cgc completion fish --install    # append a source line to ~/.config/fish/config.fish
   ```

   Supported shells are `bash`, `zsh`, and `fish`. An unsupported or undetectable shell fails fast with a clear message and a non-zero exit code. With `--install`, the line evaluates the program's live completion source on shell start-up, so completion always matches the installed CLI version.

Once installed, tab completion suggests **commands, sub-groups, options, and arguments**, plus dynamic values:

| Where | Completes |
|---|---|
| `cgc config get <key>` / `cgc config set <key>` | Known config keys (with their `CGC_*` env-var hints). |
| `cgc chat send --status <TAB>` | `info`, `working`, `success`, `error`, `blocked`. |
| `cgc completion <shell>` / `--shell` | `bash`, `zsh`, `fish`. |
| `cgc auth login --client-file <TAB>` | File paths (native shell file completion). |
| `cgc listen --space-id` | The `space_id` from your current config, if set. |
| `cgc clear --trigger-prefix` | The `trigger_prefix` from your current config. |

Dynamic completers never crash your shell: any error simply yields no suggestions.

---

## Plugin commands (inside Claude Code)

### `/claude-google-chat:chat-setup`

Interactive one-time setup. It:

1. Verifies `cgc` is on your `PATH` (and prints install instructions if not).
2. Shows current config and which env vars / config keys are required.
3. Walks you through `CGC_WEBHOOK_URL`, `CGC_SPACE_ID`, the OAuth client file path, and `CGC_TRIGGER_PREFIX`, writing them via `cgc config set`.
4. Runs `cgc auth login` if inbound reading is desired.
5. Verifies with a test send (success prints `sent`; failure exits non-zero with the status code).

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

This runs `cgc chat send --status "<status>" --text "<text>"`; on success it prints `sent`, and on failure it exits non-zero with the HTTP status code and a redacted URL.

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
- It filters to messages whose text starts with the trigger prefix — unless `require_trigger = false`, in which case every HUMAN message is surfaced (bots/own posts always excluded).
- An **idle timeout** (`CGC_LISTEN_TIMEOUT`, default `0` = run forever) causes a **fail-fast non-zero exit** with a clear diagnostic when no qualifying message arrives within the window.
- **Transient backend errors are survived**: a socket/connection timeout, dropped connection, or Chat API `408`/`429`/`5xx` is logged to stderr and the loop continues. A fatal `401`/`403` fails fast immediately. After `max_consecutive_errors` consecutive transient failures the loop fails fast (the counter resets on any success).
- The high-water marker is **persisted to `state_file`**, so a restart resumes from the last-processed message and never re-emits already-seen history.
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
  "correlation_id": null,
  "thread_name": null,
  "session_name": null
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
- `thread_name` — optional Chat thread resource name (`spaces/.../threads/...`) the message belongs to, surfaced on inbound `cgc listen` events so a consumer knows which thread a message is in; `null` when unthreaded.
- `session_name` — optional session name a routed event was delivered to by `cgc listen --session NAME`; surfaced so a consumer knows which session owns the message. `null` for non-session (plain) listening.

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
claude: deploy prod --force
```

`parse_message` reads this as `kind == "command"`, `command == "deploy"`, `args == ["prod", "--force"]`. The trigger prefix is configurable via `CGC_TRIGGER_PREFIX`.

`parse_message` accepts either a fenced JSON envelope or a trigger-prefixed plain line, validates `version`, `kind`, and `status`, and raises `ValueError` with a clear message on invalid input — it never silently falls back.

---

## Examples by kind

By default the message posted into Chat is the clean summary line alone. The fenced JSON block shown under each example below is the machine-readable envelope — it is emitted on `cgc listen` stdout (JSONL), and is only embedded in the Chat text when `send_envelope` is enabled (or `cgc chat send --envelope`).

**Status** (Claude → space) — clean Chat text, with the corresponding envelope:

```
⏳ Running tests
```json
{"version":"1","kind":"status","status":"working","text":"Running tests","command":null,"args":[],"ts":"2026-06-19T12:00:00Z","correlation_id":null}
```
```

**Command** (human → space):

```
claude: rerun-ci --branch main
```

**Result** (Claude → space, linked to a command) — clean Chat text, with the corresponding envelope:

```
✅ CI rerun complete
```json
{"version":"1","kind":"result","status":"success","text":"CI rerun complete","command":null,"args":[],"ts":"2026-06-19T12:05:00Z","correlation_id":"abc123"}
```
```

See [configuration.md](configuration.md) for tuning the trigger prefix, poll interval, and timeouts.
