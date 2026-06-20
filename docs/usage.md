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
cgc config set trigger_prefix "claude-command:"
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

### `cgc listen`

Start the inbound listener.

```bash
cgc listen                          # run forever; idle timeout from CGC_LISTEN_TIMEOUT (0 = forever)
cgc listen --once                   # drain currently-pending messages and exit (for hooks/CI)
cgc listen --timeout 300            # exit non-zero if idle for 300 seconds
cgc listen --space-id spaces/AAAA   # override the configured space for this run
```

Each new message is emitted as a single JSON line to stdout. Only messages whose text starts with the configured trigger prefix (default `claude-command:`) are surfaced as commands. `--space-id` overrides the configured `space_id` for one run; required keys are still checked and fail fast when missing.

### `cgc setup`

Print the config file location and the keys required for each operation.

```bash
cgc setup
```

### `cgc bootstrap`

Service-account (app-auth) setup that Terraform cannot do: join or create the
Chat space, register the Workspace Events `message.created` subscription to the
Pub/Sub topic, and merge the discovered values into `config.toml`. Requires
`service_account_file` and `pubsub_topic`. See the [Setup Runbook](SETUP.md) for
the full app-auth path.

```bash
cgc bootstrap
```

### `cgc serve`

Run the always-listening responder, replying to owner messages as the app (service-account auth).

```bash
cgc serve                           # run forever
cgc serve --once                    # handle pending owner messages once and exit
cgc serve --timeout 600             # exit non-zero if idle for 600 seconds
cgc serve --space-id spaces/AAAA    # override the configured space for this run
```

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
| `cgc serve --space-id` / `cgc listen --space-id` | The `space_id` from your current config, if set. |
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

---

## Phone notifications (avoid duplicate alerts)

When you send status pings to a Google Chat space, that space can reach your phone through **two independent paths**:

1. The **standalone Google Chat app** (the dedicated "Google Chat" mobile app), and
2. **Gmail's built-in Chat** (Chat is also surfaced inside the Gmail mobile app).

If both are installed and both have notifications enabled, every ping arrives **twice** on your phone. Pick exactly one of the options below so you get a single alert per ping.

### Option A — Use the standalone Google Chat app, silence Gmail's Chat

Use this if you want a dedicated Chat experience separate from email.

1. Install/keep the **Google Chat** app and sign in to the same account.
2. In the **Google Chat** app, enable notifications for the space you post pings to (open the space → notification settings → "All messages" / "Notify always", as you prefer).
3. In the **Gmail** app, turn **off** Chat notifications so it does not double-alert:
   - Gmail → menu → **Settings** → select the account → **Chat notifications** (or **Chat**) → set to **Off** / "None".
   - On desktop/web: Gmail → ⚙ **See all settings** → **Chat and Meet** → turn **Chat notifications** off, or set **Chat** to "Off" entirely if you do not use Chat inside Gmail.

Result: pings notify only through the standalone Google Chat app.

### Option B — Use Gmail only, remove/silence the standalone app

Use this if you prefer to keep everything inside Gmail and not run a second app.

1. In the **Gmail** app, make sure **Chat** is enabled and Chat notifications are **on** for the space:
   - Gmail → menu → **Settings** → select the account → **Chat notifications** → **On**, and confirm the space's per-space setting is "All messages".
2. **Remove or silence the standalone Google Chat app** so it does not also alert:
   - Either uninstall the **Google Chat** app, or
   - Open the **Google Chat** app → notification settings → set notifications to **Off** for that account/space.

Result: pings notify only through Gmail.

> The per-space notification level (All messages / Mentions only / Off) lives **inside the space** and is shared by both surfaces, while "which app alerts me" is the **app-level** toggle above. Set the space to "All messages" so pings are delivered, then use the app-level toggles so only **one** app actually rings.
