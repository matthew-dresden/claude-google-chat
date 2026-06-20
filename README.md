# ⚠️ NOT READY — DO NOT USE ⚠️

> [!CAUTION]
> ## 🚧 THIS PROJECT IS NOT READY FOR ANY USE. DO NOT INSTALL OR RELY ON IT. 🚧
>
> **This repository is experimental, incomplete, and under active development. Everything is subject to change without notice — APIs, commands, config, behavior, and documentation can break or disappear at any time.**
>
> - ❌ **Do NOT install it.** Do NOT use it in any project, automation, or production system.
> - ❌ **Do NOT rely on it.** There are no stability or compatibility guarantees of any kind.
> - 📝 **The README will be rewritten** and a stable **v1.0.0** release will be cut when — and only when — the project is actually ready for use.
>
> **Until a tagged `v1.0.0` release exists, treat everything below as a work-in-progress draft, not instructions you should follow.**

---

# claude-google-chat

> Two-way Google Chat ChatOps integration for Claude Code — a Claude Code plugin plus a Python CLI (`cgc`).

[![CI](https://github.com/matthew-dresden/claude-google-chat/actions/workflows/ci.yml/badge.svg)](https://github.com/matthew-dresden/claude-google-chat/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/claude-google-chat.svg)](https://pypi.org/project/claude-google-chat/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-informational.svg)](CHANGELOG.md)

`claude-google-chat` lets Claude Code and your team exchange **status pings, commands, and results** through a Google Chat space using a single, unambiguous structured message format.

- **Outbound status pings** to a Google Chat space via an **incoming webhook** (no OAuth required for send-only).
- **Inbound commands/messages** read from the space via the **Google Chat REST API** (OAuth user credentials).
- An **event-driven listener** that polls the space and surfaces new messages prefixed with a configurable `claude:` trigger.
- A **structured message format** so Claude Code and humans exchange status, commands, and results unambiguously.

---

## How it works

A single, session-bound path drives the whole integration:

- **Outbound status pings** go through a Google Chat **incoming webhook** (no auth for send-only), driven by `cgc chat send`.
- **Inbound commands** are read from the space via the **Google Chat REST API** using your **user OAuth** credentials, surfaced by `cgc listen`.

One CLI, one config, one structured message format in both directions. Start with the [Quickstart](#quickstart) below and [Installation](docs/installation.md).

---

## Why

When Claude Code runs long or autonomous tasks, you need a way to (a) see what it is doing without watching a terminal and (b) hand it new instructions from wherever you are. Google Chat is already where many teams live.

`claude-google-chat` provides that two-way channel:

- Claude posts structured **status** updates (`info`, `working`, `success`, `error`, `blocked`) to a shared space.
- Humans reply with `claude: <command> [args...]` lines that the listener surfaces back to Claude.
- The same envelope is used in both directions, so machines and people read the same source of truth.

Send-only operation needs nothing but an incoming webhook URL. Reading inbound commands uses OAuth user credentials scoped to Chat messages. No hardcoded secrets — everything comes from environment variables or a user config file.

---

## Quickstart

```bash
# 1. Install the CLI (pipx recommended)
pipx install claude-google-chat

# 2. Configure (webhook URL, space, OAuth, trigger prefix)
cgc config init
cgc config set webhook_url "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."

# 3. Send a status ping
cgc chat send --status success --text "Build is green"

# 4. (optional) Authenticate and start the inbound listener
cgc auth login
cgc listen

# 5. (optional) Enable shell tab completion
cgc completion bash --install   # or: zsh / fish, or `cgc --install-completion`
```

Tab completion covers commands, options, and dynamic values (config keys, `--status` labels, shell names, file paths, and config-derived `space_id`/`trigger_prefix`). See the [Shell completion guide](docs/SHELL_COMPLETION.md) for bash/zsh setup (auto-updating and static-file installs) and prerequisites.

From inside Claude Code, run the setup command and you are ready:

```
/claude-google-chat:chat-setup
```

---

## Install

`claude-google-chat` ships two things from one codebase: a **Claude Code plugin** and a **Python CLI**. The plugin commands shell out to the CLI, so install both.

### As a Claude Code plugin

```
/plugin marketplace add matthew-dresden/claude-google-chat
/plugin install claude-google-chat@claude-google-chat
```

The marketplace name and the plugin name are both `claude-google-chat`, so the install selector is `claude-google-chat@claude-google-chat`.

The plugin commands invoke the `cgc` CLI, so install the CLI as well (below). The `/claude-google-chat:chat-setup` command checks for `cgc` on your `PATH` and tells you how to install it if it is missing.

### As a CLI (Python)

**pipx (recommended):**

```bash
pipx install claude-google-chat        # from PyPI once published
cgc --help
```

pipx installs the CLI into its own isolated environment and puts `cgc` on your `PATH` — the recommended way to run a Python command-line tool.

<details>
<summary>From source (for development)</summary>

```bash
git clone https://github.com/matthew-dresden/claude-google-chat
cd claude-google-chat
uv sync && uv run cgc --help
```
</details>

See [docs/installation.md](docs/installation.md) for the full Google Cloud setup (OAuth client + incoming webhook) and prerequisites.

---

## Configuration

Configuration is **env-first**. Precedence (highest first): explicit CLI flag → environment variable → user config file → error if a required value is missing (no defaults for secrets; fail fast).

The user config file lives in your OS config directory (resolved via `platformdirs`), never inside the repo or working directory:

- Linux: `~/.config/claude-google-chat/config.toml`
- macOS: `~/Library/Application Support/claude-google-chat/config.toml`
- Windows: `%LOCALAPPDATA%\claude-google-chat\config.toml`

| Setting · env var | Description |
| --- | --- |
| **`webhook_url`**<br>`CGC_WEBHOOK_URL` | Google Chat incoming webhook URL. **Required** for `send`. |
| **`space_id`**<br>`CGC_SPACE_ID` | Chat space id, e.g. `spaces/AAAA`. **Required** for read/listen. |
| **`oauth_client_file`**<br>`CGC_OAUTH_CLIENT_FILE` | Path to Google OAuth client secrets JSON. **Required** for read/listen. |
| **`token_file`**<br>`CGC_TOKEN_FILE` | Cached OAuth user token (path). Optional · default `<config_dir>/token.json`. |
| **`trigger_prefix`**<br>`CGC_TRIGGER_PREFIX` | Inbound command trigger. Optional · default `claude:`. |
| **`poll_interval`**<br>`CGC_POLL_INTERVAL` | Listener poll interval, seconds (float). Optional · default `2.0`. |
| **`listen_timeout`**<br>`CGC_LISTEN_TIMEOUT` | Listener idle timeout, seconds (float); governs `listen`. Optional · default `0` (run forever). |
| **`send_envelope`**<br>`CGC_SEND_ENVELOPE` | Append the machine-readable JSON envelope to outbound Chat text. Optional · default `false` (clean human-facing summary only). |
| **`max_consecutive_errors`**<br>`CGC_MAX_CONSECUTIVE_ERRORS` | Consecutive transient poll failures (`listen`) tolerated before the loop fails fast with a non-zero exit. The counter resets on any successful poll (int). Optional · default `10`. |
| **`state_file`**<br>`CGC_STATE_FILE` | Durable high-water state path for `listen`. Records the last-processed message time so a restart resumes instead of re-emitting recent history (written `0600`). Optional · default `<config_dir>/listen-state.json`. |
| **`require_trigger`**<br>`CGC_REQUIRE_TRIGGER` | When `true` (default), `listen` emits only messages starting with `trigger_prefix`. When `false`, `listen` surfaces **every** message from a HUMAN sender (bots/own posts always excluded) — trigger-prefixed lines still parse as commands; plain lines are surfaced as a message carrying the full text. Boolean. Optional · default `true`. |
| **`threads`**<br>`CGC_THREADS` | Optional thread filter for `listen`: when set, only messages whose `thread.name` is in this set are emitted (composes with the trigger/sender rules). TOML array of thread resource names (`spaces/.../threads/...`) in config; comma-separated list as `CGC_THREADS`; or per-run `cgc listen --thread <NAME>` (repeatable). Optional · default empty (no filter). |
| **`sessions_file`**<br>`CGC_SESSIONS_FILE` | Durable **session registry** for `cgc connect` / `session list` / `disconnect` and `cgc listen --session`. JSON map of session name → space, claimed threads, dispatcher flag, `created_at` (written `0600`, no secrets). Optional · default `<config_dir>/sessions.json`. |

Secrets are never echoed: `cgc config show` masks the webhook token and token-file contents. See [docs/configuration.md](docs/configuration.md) for details.

**Human vs. machine views.** By default outbound Chat messages (`cgc chat send`) are the clean, emoji-prefixed summary line alone — the JSON envelope is **not** posted into the human-facing Chat view. The machine-readable channel is the JSONL written to stdout by `cgc listen` (one envelope per line). To additionally embed the JSON envelope in the Chat text, opt in with `send_envelope = true` (or `CGC_SEND_ENVELOPE=true`), or per send with `cgc chat send --envelope`.

**Thread routing.** Post into a specific thread with `cgc chat send --thread-key <KEY>` (same key → same thread; the created `thread.name` is printed to stderr for read-filtering). Read only specific threads with `cgc listen --thread <THREAD_NAME>` (repeatable; or config `threads` / `CGC_THREADS`). Each emitted `cgc listen` event carries a `thread_name` field naming the owning thread. See [docs/usage.md](docs/usage.md) and [docs/configuration.md](docs/configuration.md).

**Sessions.** On top of the thread primitives, the **session layer** binds a named working context (git repo + branch + cwd) to its Chat threads in the shared space so multiple Claude Code instances can share one space:

- `cgc connect [NAME] [--space SPACE] [--dispatcher]` — create/reuse a session (deriving a stable `NAME` from git + cwd when omitted) and open its primary thread. The first session auto-becomes the **dispatcher**; reconnecting is idempotent.
- `cgc session list` — show sessions, their threads, and which is the dispatcher.
- `cgc disconnect NAME [--notify]` — remove a session (promoting a new dispatcher if needed).
- `cgc listen --session NAME` — routing-aware listen: emit replies in `NAME`'s claimed threads, **claim+emit** a new `NAME: ...` thread (prefix stripped), and (if dispatcher) answer truly-unrouted new threads with a "which session?" menu. Each routed event carries `session_name` + `thread_name`. State lives in `sessions_file`. See [docs/usage.md](docs/usage.md).

---

## Usage

### Setup (inside Claude Code)

```
/claude-google-chat:chat-setup
```

Walks you through providing the webhook URL, space id, OAuth client file, and trigger prefix; writes them to the user config dir via `cgc config set`; optionally runs `cgc auth login`; and verifies with a test send.

### Send a status ping

```
/claude-google-chat:chat-send success Build is green
```

or directly with the CLI:

```bash
cgc chat send --status working --text "Running tests"
```

### Start the listener

```
/claude-google-chat:chat-listener
```

or directly:

```bash
cgc listen                 # run forever (idle timeout from CGC_LISTEN_TIMEOUT)
cgc listen --once          # drain currently-pending messages and exit (for hooks/CI)
cgc listen --timeout 300   # exit non-zero if idle for 300s
```

Each emitted line is a structured JSON message. Inbound messages are surfaced when their text starts with the configured trigger prefix (default `claude:`).

### Sessions (multi-instance routing)

```bash
cgc connect myapp            # open a session + its primary thread (first = dispatcher)
cgc session list             # list sessions and the dispatcher
cgc listen --session myapp   # route: replies in my threads, 'myapp:' claims a new thread
cgc disconnect myapp         # remove the session
```

### Clear / housekeeping

The CLI exposes message management through the Chat API (used by the listener and available for cleanup). See [docs/usage.md](docs/usage.md) for the full command reference and structured message examples.

---

## Architecture

`claude-google-chat` is one Python package (`claude_google_chat`) plus a thin Claude Code plugin layer.

- `messages.py` — pure, I/O-free structured message envelope (`format_message` / `parse_message` / `to_jsonl`); single source of truth for the protocol.
- `validation.py` — pure shared format validators (`validate_space_id`, `validate_create_time`).
- `config.py` — single config authority; merges file + env, validates, fails fast on missing required values.
- `auth.py` — Google user OAuth (InstalledAppFlow) for read/listen; never logs token material.
- `chat.py` — webhook send + Chat API list/delete (user OAuth).
- `polling.py` — shared poll primitive (dedup, high-water tracking, idle-timeout loop, JSON-line emit) used by `listener.py`.
- `rawmessage.py` — pure accessors for raw Chat `messages.list` resources (HUMAN/BOT sender gating for loop prevention).
- `resilience.py` — transient-vs-fatal poll-error classification.
- `state.py` — durable high-water `StateStore` so a restart resumes instead of re-emitting.
- `listener.py` — event/poll-driven listener with env-driven cadence and idle timeout (no `sleep` as a readiness primitive).
- `cli.py` — Typer app exposing `cgc` (`config init|show|get|set`, `auth login`, `chat send`, `listen`, `clear`, `status`, `setup`, `completion`).
- `__main__.py` — `python -m claude_google_chat` entry point.

Data flow: Claude Code → `/claude-google-chat:*` command → `cgc` CLI → Google Chat (incoming webhook for outbound sends; Chat REST API with user OAuth for inbound reads). See [docs/architecture.md](docs/architecture.md) for the full breakdown and diagram.

---

## Documentation

- [Installation](docs/installation.md) — install paths plus Google Cloud setup (OAuth client + incoming webhook).
- [Usage](docs/usage.md) — command reference, listener behavior, message examples.
- [Configuration](docs/configuration.md) — full config table, precedence, secret handling.
- [Shell completion](docs/SHELL_COMPLETION.md) — bash/zsh tab completion: auto-updating and static-file installs, prerequisites.
- [Architecture](docs/architecture.md) — module responsibilities, data flow, protocol.
- [Contributing](CONTRIBUTING.md) — dev setup and conventions.
- [Changelog](CHANGELOG.md) — release history.

---

## License

Licensed under the [Apache License, Version 2.0](./LICENSE).

Copyright 2026 Matthew Dresden.
