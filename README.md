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

## Two ways to run this

There are two authentication paths; pick the one that matches your use case:

1. **Quickstart path — webhook + user OAuth.** Outbound status pings go through an **incoming webhook** (no auth for send-only); inbound reading uses **user OAuth** credentials. Driven by `cgc chat send` and `cgc listen`. Start with the [Quickstart](#quickstart) below and [Installation](docs/installation.md).
2. **App-auth path — service account.** The bot posts and reads as the **Chat app** itself, authenticated by a **service account**. Infrastructure is provisioned with Terraform, wired up by `cgc bootstrap`, and run with `cgc serve` — no user OAuth and no webhooks. The **[Setup Runbook](docs/SETUP.md)** is the canonical guide for this path.

The two paths share the same CLI, config, and structured message format. The quickstart path is the fastest way to send a ping; the app-auth path is the full two-way integration.

---

## Why

When Claude Code runs long or autonomous tasks, you need a way to (a) see what it is doing without watching a terminal and (b) hand it new instructions from wherever you are. Google Chat is already where many teams live.

`claude-google-chat` provides that two-way channel:

- Claude posts structured **status** updates (`info`, `working`, `success`, `error`, `blocked`) to a shared space.
- Humans reply with `claude: <command> [args...]` lines that the listener surfaces back to Claude.
- The same envelope is used in both directions, so machines and people read the same source of truth.

Send-only operation needs nothing but an incoming webhook URL. Reading inbound commands uses OAuth user credentials scoped to Chat messages. No hardcoded secrets — everything comes from environment variables or a user config file.

---

## Setup

> **Standing it up from scratch?** Follow the **[Setup Runbook](docs/SETUP.md)** —
> a brain-dead-simple, fully numbered, zero-to-working guide for the
> service-account (app-auth) design. Every step is marked
> **[AUTOMATED: terraform]**, **[AUTOMATED: cgc]**, or **[MANUAL: you]** with the
> exact command or console clicks, including the one irreducible manual step
> (the Google Chat API Configuration page) and `terraform destroy` teardown.

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
| **`listen_timeout`**<br>`CGC_LISTEN_TIMEOUT` | Listener/responder idle timeout, seconds (float); governs `listen` and `serve`. Optional · default `0` (run forever). |
| **`send_envelope`**<br>`CGC_SEND_ENVELOPE` | Append the machine-readable JSON envelope to outbound Chat text. Optional · default `false` (clean human-facing summary only). |

Secrets are never echoed: `cgc config show` masks the webhook token and token-file contents. See [docs/configuration.md](docs/configuration.md) for details.

**Human vs. machine views.** By default outbound Chat messages (`cgc chat send` and `cgc serve` replies) are the clean, emoji-prefixed summary line alone — the JSON envelope is **not** posted into the human-facing Chat view. The machine-readable channel is the JSONL written to stdout by `cgc listen` / `cgc serve` (one envelope per line). To additionally embed the JSON envelope in the Chat text, opt in with `send_envelope = true` (or `CGC_SEND_ENVELOPE=true`), or per send with `cgc chat send --envelope`.

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

### Clear / housekeeping

The CLI exposes message management through the Chat API (used by the listener and available for cleanup). See [docs/usage.md](docs/usage.md) for the full command reference and structured message examples.

---

## Architecture

`claude-google-chat` is one Python package (`claude_google_chat`) plus a thin Claude Code plugin layer.

- `messages.py` — pure, I/O-free structured message envelope (`format_message` / `parse_message` / `to_jsonl`); single source of truth for the protocol.
- `validation.py` — pure shared format validators (`validate_space_id`, `validate_create_time`).
- `config.py` — single config authority; merges file + env, validates, fails fast on missing required values.
- `auth.py` — Google user OAuth (InstalledAppFlow) for read/listen and service-account (app) auth for bootstrap/serve; never logs tokens or key material.
- `chat.py` — webhook send + Chat API list/delete (user OAuth) and post/list as the app (service account).
- `polling.py` — shared poll primitive (dedup, high-water tracking, idle-timeout loop, JSON-line emit) used by `listener.py` and `serve.py`.
- `listener.py` — event/poll-driven listener with env-driven cadence and idle timeout (no `sleep` as a readiness primitive).
- `bootstrap.py` — service-account setup Terraform can't do (join/create space, register the Workspace Events subscription, merge config).
- `serve.py` — always-listening responder that replies to owner messages as the app.
- `cli.py` — Typer app exposing `cgc` (`config init|show|get|set`, `auth login`, `chat send`, `bootstrap`, `serve`, `listen`, `clear`, `status`, `setup`, `completion`).
- `__main__.py` — `python -m claude_google_chat` entry point.

Data flow: Claude Code → `/claude-google-chat:*` command → `cgc` CLI → Google Chat (incoming webhook or Chat REST API for the quickstart path; Chat REST API as the service account for the app-auth path). See [docs/architecture.md](docs/architecture.md) for the full breakdown and diagram.

---

## Documentation

- [Setup Runbook](docs/SETUP.md) — **canonical app-auth guide:** numbered zero-to-working runbook (terraform + `cgc bootstrap`/`serve` + the one manual console step), with a what's-automated-vs-manual table and teardown.
- [Installation](docs/installation.md) — both install paths plus Google Cloud setup.
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
