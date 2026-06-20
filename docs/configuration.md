# Configuration

`claude-google-chat` is **env-first** and never hardcodes secrets, paths, or timeouts. All configuration comes from environment variables or a user config file; required values that are missing cause a **fail-fast** error.

---

## Precedence

From highest priority to lowest:

1. **Explicit CLI flag** (e.g. `cgc listen --timeout 300`).
2. **Environment variable** (e.g. `CGC_TRIGGER_PREFIX`).
3. **User config file** (`config.toml` in the OS config dir).
4. **Error if a required value is missing** — there are no defaults for secrets.

Non-secret tunables (trigger prefix, listen timeout, poll interval) have documented defaults and never error.

---

## Config file location

The user config file is resolved via `platformdirs.user_config_path("claude-google-chat") / "config.toml"`. It is **never** read from or written to the repository or the current working directory.

| OS | Path |
|---|---|
| Linux | `~/.config/claude-google-chat/config.toml` |
| macOS | `~/Library/Application Support/claude-google-chat/config.toml` |
| Windows | `%LOCALAPPDATA%\claude-google-chat\config.toml` |

Create it with:

```bash
cgc config init
```

The config file is read with the Python 3.11+ stdlib `tomllib` module. Writes from `cgc config set` use a minimal serializer (no third-party TOML writer required), keeping the dependency surface small; the `tomllib`-based read path is the tested one.

---

## Reference

| Setting · env var | Description |
| --- | --- |
| **`webhook_url`**<br>`CGC_WEBHOOK_URL` | Google Chat incoming webhook URL. **Required** for `send`. |
| **`space_id`**<br>`CGC_SPACE_ID` | Chat space id, e.g. `spaces/AAAA`. **Required** for read/listen. |
| **`oauth_client_file`**<br>`CGC_OAUTH_CLIENT_FILE` | Path to Google OAuth client secrets JSON. **Required** for read/listen. |
| **`token_file`**<br>`CGC_TOKEN_FILE` | Cached OAuth user token (path). Optional · default `<config_dir>/token.json`. |
| **`trigger_prefix`**<br>`CGC_TRIGGER_PREFIX` | Inbound command trigger. Optional · default `claude:`. |
| **`poll_interval`**<br>`CGC_POLL_INTERVAL` | Listener poll interval, seconds (float). Optional · default `2.0`. |
| **`listen_timeout`**<br>`CGC_LISTEN_TIMEOUT` | Listener idle timeout, seconds (float). Optional · default `0` (run forever). |
| **`webhook_timeout`**<br>`CGC_WEBHOOK_TIMEOUT` | Outbound webhook HTTP timeout, seconds (float). Optional · default `30.0`. |
| **`page_size`**<br>`CGC_PAGE_SIZE` | Chat API `messages.list` page size (int). Optional · default `100`. |
| **`send_envelope`**<br>`CGC_SEND_ENVELOPE` | Append the machine-readable JSON envelope to outbound Chat text (`cgc chat send`). Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`, case-insensitive; unparseable values fail fast). Optional · default `false` — the human-facing Chat view is the clean summary line only. The machine channel is the JSONL on `cgc listen` stdout. Override per send with `cgc chat send --envelope` / `--no-envelope`. |
| **`max_consecutive_errors`**<br>`CGC_MAX_CONSECUTIVE_ERRORS` | Number of **consecutive** transient poll failures (`listen`) tolerated before the loop fails fast with a non-zero exit and a clear diagnostic. Transient errors (socket/connection timeouts, dropped connections, Chat API `408`/`429`/`5xx`) are logged and skipped; the counter resets to zero on any successful poll, so isolated hiccups never trip it but a truly-down backend still surfaces. Integer (unparseable values fail fast). Optional · default `10`. |
| **`state_file`**<br>`CGC_STATE_FILE` | Path to the durable high-water state file for `listen`. On startup the last-processed message time is loaded from it; on each emitted/seen message it is updated and persisted (written with `0600` permissions). This makes a restart **resume** from the last processed message instead of re-reading recent history and re-emitting already-seen messages. A missing or corrupt file is treated as a fresh start (never a crash). Optional · default `<config_dir>/listen-state.json`. |
| **`require_trigger`**<br>`CGC_REQUIRE_TRIGGER` | Controls which inbound messages `cgc listen` emits. When `true` (default, current behavior) only messages whose text starts with `trigger_prefix` are emitted (parsed as commands). When `false` (catch-all mode) **every** message from a HUMAN sender is surfaced regardless of prefix — trigger-prefixed lines still parse as structured commands, while plain conversational lines are surfaced as a message carrying the full text. Non-human senders (BOT/app/webhook) are always excluded so the listener never echoes its own outbound posts or other bots (loop prevention). Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`, case-insensitive; unparseable values fail fast). Optional · default `true`. |

### Which keys are required when

- **Send only** (outbound webhook): `webhook_url`.
- **Read / listen** (inbound API, user OAuth): `space_id` and `oauth_client_file` (plus a cached token from `cgc auth login`).

Operations request exactly the keys they need. For example, a read operation loads config with `Config.load(require=("space_id",))`; if `space_id` is missing, the loader raises a clear error naming the missing key and exits non-zero.

---

## Example `config.toml`

```toml
webhook_url = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=REDACTED&token=REDACTED"
space_id = "spaces/AAAA"
oauth_client_file = "/home/you/.config/claude-google-chat/oauth_client.json"
trigger_prefix = "claude:"
poll_interval = 2.0
listen_timeout = 0
# send_envelope = false  # default: keep human Chat messages clean (opt in to embed JSON)
```

Equivalent environment configuration:

```bash
export CGC_WEBHOOK_URL="https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
export CGC_SPACE_ID="spaces/AAAA"
export CGC_OAUTH_CLIENT_FILE="/path/to/oauth_client.json"
export CGC_TRIGGER_PREFIX="claude:"
export CGC_POLL_INTERVAL="2.0"
export CGC_LISTEN_TIMEOUT="0"
# export CGC_SEND_ENVELOPE="false"  # default; set true to embed the JSON envelope in Chat text
```

---

## Secret handling

- **Secrets are never echoed.** `cgc config show` masks the webhook URL token and the cached token path, and never prints their contents.
- **Secrets are never logged.** No module logs the webhook URL, OAuth client contents, or user token.
- **The cached OAuth token** is written with restrictive (`0600`) file permissions by `cgc auth login`.
- **Nothing secret is stored in the repo.** Config lives in the OS config dir; `token.json`, `.env`, and `*.local.*` are gitignored.
- **No fallbacks for secrets.** A missing required secret is a fail-fast error, not a silent default.

`Config.redacted()` (used by `cgc config show`) produces a view safe to print, with the webhook token reduced to a masked placeholder.

---

## Timeouts and cadence

- `poll_interval` is the **deliberate, env-driven cadence** at which the listener polls the space. It is not a `sleep`-based readiness wait.
- `listen_timeout` is an **idle timeout**. When set to a positive value, the listener exits non-zero with a clear diagnostic if no qualifying message arrives within the window. `0` means run forever.
- `webhook_timeout` is the **outbound HTTP timeout** for the incoming-webhook `POST` in `cgc chat send`. It bounds a single network call, not a cadence.
- `page_size` is the **Chat API list page size** used when reading messages (`listen`/`clear`).

All four are configurable via environment variable or the config file; none is hardcoded.

---

## Resilience and durable state

- `max_consecutive_errors` bounds how many **consecutive** transient poll failures the `listen` loop absorbs before failing fast. Transient errors (socket/connection timeouts, dropped connections, Chat API `408`/`429`/`5xx`) are logged to stderr as a concise, secret-free diagnostic and the loop continues on the normal cadence; a fatal auth/permission error (`401`/`403`) always fails fast immediately. The counter resets on any successful poll.
- `state_file` makes the poll high-water marker **durable**: a restart resumes from the last-processed message instead of re-reading recent history and re-emitting already-seen messages. The file is written with `0600` permissions; a missing or corrupt file degrades to a fresh start (never a crash).

Neither value is hardcoded; both come from the environment or the config file.

---

See [usage.md](usage.md) for how these values affect command behavior, and [architecture.md](architecture.md) for where `config.py` sits in the system.
