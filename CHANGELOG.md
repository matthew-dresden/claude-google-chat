# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-19

### Added

- **Claude Code plugin** installable from a marketplace (`/plugin marketplace add matthew-dresden/claude-google-chat`, `/plugin install claude-google-chat@claude-google-chat`), with the `.claude-plugin/plugin.json` and `marketplace.json` manifests.
- **Slash commands:**
  - `/claude-google-chat:chat-setup` — interactive configuration helper (webhook URL, space, OAuth, trigger prefix).
  - `/claude-google-chat:chat-send` — send a structured status ping to the configured Google Chat space via the incoming webhook.
  - `/claude-google-chat:chat-listener` — start the event-driven inbound listener and surface trigger-prefixed commands.
- **`google-chat` skill** documenting the ChatOps protocol: the structured message envelope, the on-the-wire representation, the `claude-command:` trigger, and the operational rules.
- **`Stop` hook** (`hooks/hooks.json`) that sends a status ping to the configured Google Chat space when a Claude Code session stops.
- **Python CLI `cgc`** (Typer) with subcommands `config init|show|set`, `auth login`, `chat send`, and `listen`, plus `--version`.
- **Structured message format** (`messages.py`): frozen `ChatMessage` dataclass with pure `format_message` / `parse_message` functions, strict validation of `version`/`kind`/`status`, and a single status→emoji map.
- **Env-first configuration** (`config.py`): file + environment merge with documented precedence, fail-fast on missing required values, defaults for non-secret tunables, OS config-dir storage via `platformdirs`, and secret-masking for display.
- **Google OAuth** (`auth.py`) via InstalledAppFlow with locally cached tokens written with restrictive permissions, used for inbound read/listen.
- **Google Chat integration** (`chat.py`): outbound send via incoming webhook, inbound list/delete via the Chat REST API.
- **Event-driven listener** (`listener.py`): env-driven poll cadence and idle timeout (no `sleep`-based readiness), `--once` drain mode, JSON-line output to stdout.
- **Documentation:** README plus `docs/installation.md`, `docs/usage.md`, `docs/configuration.md`, and `docs/architecture.md`.
- **CI and release workflows:** GitHub Actions CI (lint, format check, type check, test, build, manifest validation across Python 3.11 and 3.12) and an idempotent release workflow that tags `v<version>` and publishes built artifacts.

[Unreleased]: https://github.com/matthew-dresden/claude-google-chat/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/matthew-dresden/claude-google-chat/releases/tag/v0.1.0
