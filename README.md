# ⚠️ NOT READY — DO NOT USE ⚠️

> [!CAUTION]
> ## 🚧 THIS PROJECT IS NOT READY FOR ANY USE. DO NOT INSTALL OR RELY ON IT. 🚧
>
> **This repository is experimental, incomplete, and under active development. Everything is subject to change without notice — APIs, commands, config, behavior, and documentation can break or disappear at any time.**
>
> - ❌ **Do NOT install it.** Do NOT use it in any project, automation, or production system.
> - ❌ **Do NOT rely on it.** There are no stability or compatibility guarantees of any kind.
> - ℹ️ **Why is it public, then?** This repo is public **only** to use GitHub's free features (Actions/CI, Pages, etc.) during development. Public visibility is **not** an endorsement that it works or is ready.
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
[![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-informational.svg)](./CHANGELOG.md)

`claude-google-chat` lets Claude Code and your team exchange **status pings, commands, and results** through a Google Chat space using a single, unambiguous structured message format.

- **Outbound status pings** to a Google Chat space via an **incoming webhook** (no OAuth required for send-only).
- **Inbound commands/messages** read from the space via the **Google Chat REST API** (OAuth user credentials).
- An **event-driven listener** that polls the space and surfaces new messages prefixed with a configurable `claude-command:` trigger.
- A **structured message format** so Claude Code and humans exchange status, commands, and results unambiguously.

---

## Why

When Claude Code runs long or autonomous tasks, you need a way to (a) see what it is doing without watching a terminal and (b) hand it new instructions from wherever you are. Google Chat is already where many teams live.

`claude-google-chat` provides that two-way channel:

- Claude posts structured **status** updates (`info`, `working`, `success`, `error`, `blocked`) to a shared space.
- Humans reply with `claude-command: <command> [args...]` lines that the listener surfaces back to Claude.
- The same envelope is used in both directions, so machines and people read the same source of truth.

Send-only operation needs nothing but an incoming webhook URL. Reading inbound commands uses OAuth user credentials scoped to Chat messages. No AWS/SSM dependency. No hardcoded secrets — everything comes from environment variables or a user config file.

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
```

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
<summary>Alternatives — uv, pip, or from source</summary>

```bash
# uv
uv tool install claude-google-chat

# pip
pip install claude-google-chat

# from source
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

| Key (`config.toml`) | Env var | Required | Default | Purpose |
|---|---|---|---|---|
| `webhook_url` | `CGC_WEBHOOK_URL` | yes (for send) | — | Google Chat incoming webhook URL |
| `space_id` | `CGC_SPACE_ID` | yes (for read/listen) | — | Chat space resource id (e.g. `spaces/AAAA`) |
| `oauth_client_file` | `CGC_OAUTH_CLIENT_FILE` | yes (for read/listen) | — | Path to Google OAuth client secrets JSON |
| `token_file` | `CGC_TOKEN_FILE` | no | `<config_dir>/token.json` | Cached OAuth user token |
| `trigger_prefix` | `CGC_TRIGGER_PREFIX` | no | `claude-command:` | Inbound command trigger |
| `poll_interval` | `CGC_POLL_INTERVAL` | no | `2.0` (seconds) | Listener poll interval |
| `listen_timeout` | `CGC_LISTEN_TIMEOUT` | no | `0` (0 = run forever) | Listener idle timeout |

Secrets are never echoed: `cgc config show` masks the webhook token and token-file contents. See [docs/configuration.md](docs/configuration.md) for details.

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

Each emitted line is a structured JSON message. Inbound messages are surfaced when their text starts with the configured trigger prefix (default `claude-command:`).

### Clear / housekeeping

The CLI exposes message management through the Chat API (used by the listener and available for cleanup). See [docs/usage.md](docs/usage.md) for the full command reference and structured message examples.

---

## Architecture

`claude-google-chat` is one Python package (`claude_google_chat`) plus a thin Claude Code plugin layer.

- `messages.py` — pure, I/O-free structured message envelope (`format_message` / `parse_message`); single source of truth for the protocol.
- `config.py` — single config authority; merges file + env, validates, fails fast on missing required values.
- `auth.py` — Google OAuth (InstalledAppFlow) for read/listen; caches the token with restrictive permissions.
- `chat.py` — webhook send + Chat API list/delete.
- `listener.py` — event/poll-driven listener with env-driven cadence and idle timeout (no `sleep` as a readiness primitive).
- `cli.py` — Typer app exposing `cgc` (`config`, `auth login`, `chat send`, `listen`).

Data flow: Claude Code → `/claude-google-chat:*` command → `cgc` CLI → Google Chat (incoming webhook for outbound, Chat REST API for inbound). See [docs/architecture.md](docs/architecture.md) for the full breakdown and diagram.

---

## Documentation

- [Setup Runbook](docs/SETUP.md) — **start here:** numbered zero-to-working guide (terraform + `cgc` + the one manual console step), with a what's-automated-vs-manual table and teardown.
- [Installation](docs/installation.md) — both install paths plus Google Cloud setup.
- [Usage](docs/usage.md) — command reference, listener behavior, message examples.
- [Configuration](docs/configuration.md) — full config table, precedence, secret handling.
- [Architecture](docs/architecture.md) — module responsibilities, data flow, protocol.
- [Contributing](CONTRIBUTING.md) — dev setup and conventions.
- [Changelog](CHANGELOG.md) — release history.

---

## License

Licensed under the [Apache License, Version 2.0](./LICENSE).

Copyright 2026 Matthew Dresden.
