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

| Key (`config.toml`) | Env var | Required | Default | Type | Purpose |
|---|---|---|---|---|---|
| `webhook_url` | `CGC_WEBHOOK_URL` | yes (for send) | — | string | Google Chat incoming webhook URL |
| `space_id` | `CGC_SPACE_ID` | yes (for read/listen) | — | string | Chat space resource id (e.g. `spaces/AAAA`) |
| `oauth_client_file` | `CGC_OAUTH_CLIENT_FILE` | yes (for read/listen) | — | string (path) | Path to Google OAuth client secrets JSON |
| `token_file` | `CGC_TOKEN_FILE` | no | `<config_dir>/token.json` | string (path) | Cached OAuth user token |
| `trigger_prefix` | `CGC_TRIGGER_PREFIX` | no | `claude-command:` | string | Inbound command trigger |
| `poll_interval` | `CGC_POLL_INTERVAL` | no | `2.0` | float (seconds) | Listener poll interval |
| `listen_timeout` | `CGC_LISTEN_TIMEOUT` | no | `0` | float (seconds) | Listener idle timeout (`0` = run forever) |

### Which keys are required when

- **Send only** (outbound webhook): `webhook_url`.
- **Read / listen** (inbound API): `space_id` and `oauth_client_file` (plus a cached token from `cgc auth login`).

Operations request exactly the keys they need. For example, a read operation loads config with `Config.load(require=("space_id",))`; if `space_id` is missing, the loader raises a clear error naming the missing key and exits non-zero.

---

## Example `config.toml`

```toml
webhook_url = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=REDACTED&token=REDACTED"
space_id = "spaces/AAAA"
oauth_client_file = "/home/you/.config/claude-google-chat/oauth_client.json"
trigger_prefix = "claude-command:"
poll_interval = 2.0
listen_timeout = 0
```

Equivalent environment configuration:

```bash
export CGC_WEBHOOK_URL="https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
export CGC_SPACE_ID="spaces/AAAA"
export CGC_OAUTH_CLIENT_FILE="/path/to/oauth_client.json"
export CGC_TRIGGER_PREFIX="claude-command:"
export CGC_POLL_INTERVAL="2.0"
export CGC_LISTEN_TIMEOUT="0"
```

---

## Secret handling

- **Secrets are never echoed.** `cgc config show` masks the webhook URL token and never prints token-file contents.
- **Secrets are never logged.** No module logs the webhook URL, OAuth client contents, or user token.
- **The cached OAuth token** is written with restrictive (`0600`) file permissions by `cgc auth login`.
- **Nothing secret is stored in the repo.** Config lives in the OS config dir; `token.json`, `.env`, and `*.local.*` are gitignored.
- **No fallbacks for secrets.** A missing required secret is a fail-fast error, not a silent default.

`Config.redacted()` (used by `cgc config show`) produces a view safe to print, with the webhook token reduced to a masked placeholder.

---

## Timeouts and cadence

- `poll_interval` is the **deliberate, env-driven cadence** at which the listener polls the space. It is not a `sleep`-based readiness wait.
- `listen_timeout` is an **idle timeout**. When set to a positive value, the listener exits non-zero with a clear diagnostic if no qualifying message arrives within the window. `0` means run forever.

Both are configurable; neither is hardcoded.

---

See [usage.md](usage.md) for how these values affect command behavior, and [architecture.md](architecture.md) for where `config.py` sits in the system.
