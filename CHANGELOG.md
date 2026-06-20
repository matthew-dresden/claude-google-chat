# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Robust test suite (>90% coverage)**: unit tests for every module (`auth`, `bootstrap`, `chat`, `cli`, `config`, `listener`, `messages`, `serve`), plus `tests/integration/` flow tests and `tests/journeys/` end-to-end user-journey tests. Coverage is enforced in CI via `pytest --cov` with `--cov-fail-under=90` wired into `[tool.pytest.ini_options]`; the suite currently reports 100% line coverage. Adds the `freezegun`, `pytest-cov`, and `responses` dev dependencies (in `[dependency-groups].dev`) and ignores coverage artifacts (`.coverage`, `coverage.xml`, `htmlcov/`) in `.gitignore`.
- **Automated tag release pipeline** (`.github/workflows/release.yml`): on push to `main`, re-validates and reads the version from `pyproject.toml` (single source of truth), then cuts an annotated git tag `v<version>` plus a GitHub Release carrying the built `dist/*` artifacts. Idempotent — skips cleanly when the tag already exists, so it cuts no new release without a version bump.
- **PyPI publish workflow** (`.github/workflows/publish.yml`) using `pypa/gh-action-pypi-publish` with **OIDC Trusted Publishing** (no stored token) by default, plus a documented API-token fallback. Triggers only on a published GitHub Release or manual dispatch — never on the merge-validation path — and runs in a GitHub Environment named `pypi`. Inert until the maintainer completes one of the two PyPI setup options documented in `docs/installation.md`.
- **Makefile `publish` target** for validated manual publishing: `uv build` + `uvx twine check` + `uv publish`, reading the token from `UV_PUBLISH_TOKEN` (never hardcoded). Added `format-check` and `distcheck` targets.
- **`docs/usage.md`**: "Phone notifications (avoid duplicate alerts)" section covering the standalone Google Chat app vs. Gmail Chat surfaces and how to disable one to avoid duplicate phone alerts.
- **Secret-scanning CI** (`.github/workflows/gitleaks.yml`): runs the open-source gitleaks **binary** (pinned upstream release, no licensed action) on every push and pull request via `gitleaks detect --source . --redact --exit-code 1`, so no license or stored secret is required for the public repo.
- **gitleaks configuration** (`.gitleaks.toml`): extends the default ruleset and allowlists the project's known-safe placeholders (`SECRETKEY`, `SECRETTOKEN`, `spaces/AAAA`, `key=...&token=...`, `/tmp/client_secret.json`) and `tests/data/*.toml` fixtures so real secrets are still caught without false positives.
- **Pre-commit secret scan** (`.pre-commit-config.yaml`): a local gitleaks hook mirroring the CI gate; `CONTRIBUTING.md` documents `pip install pre-commit && pre-commit install` and notes that CI enforces the same scan.
- **`.gitignore` hardening**: appended de-duped ignore patterns for credentials and local state (`config.toml`/`*config.toml`, `*.key`, `*_key.json`/`*-key.json`, `client_secret*.json`, `credentials*.json`, `service-account*.json`/`sa*.json`, `*.pem`, `.env.*`, `*.tfstate`/`*.tfstate.*`, `.terraform/`) so no secret or state can be committed, while keeping `terraform.tfvars.example` tracked.
- **README "NOT READY" banner**: a prominent warning at the very top of `README.md` stating the project is experimental, must not be installed or relied on, is public only to use GitHub's free features, and will be rewritten with a stable `v1.0.0` when ready.

### Changed

- **Pinned-action bump**: upgraded `actions/checkout@v4 -> @v5` and `astral-sh/setup-uv@v6 -> @v8` across the CI, release, and publish workflows to run on the current Node 24 runtime and clear the Node 20 deprecation warning.

- **CI** (`.github/workflows/ci.yml`) now includes a version-consistency gate (`pyproject.toml` vs `src/claude_google_chat/__init__.py:__version__`) and routes format checking through the `make format-check` target.
- **`docs/installation.md`** and **`docs/architecture.md`** document the build/release/publish pipeline, how to cut a release, how to finish PyPI setup (Trusted Publisher or API token), and the manual-publish path. `docs/installation.md` also notes the `cgc` console-command name overlap with an unrelated PyPI package and the unambiguous `python -m claude_google_chat` invocation.

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
