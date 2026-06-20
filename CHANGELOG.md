# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`COMPLIANCE.md`** at the repo root summarizing how the project meets each applicable `CLAUDE.md` rule, the justified exceptions (finance/K8s controls N/A; documented completer error-swallowing; documented `Stop`-hook `webhook_url` requirement; env-driven poll cadence), and follow-ups.
- **Shared poll primitive** (`polling.py`): `PollLoop` holds the `_seen`/`_since` dedup, `createTime` high-water tracking, idle-timeout run loop, and one-JSON-line-per-message stdout emit shared by `listener.py` and `serve.py` (removes the duplicated bookkeeping and timeout/run wrappers). `run_to_exit_code` centralizes the idle-timeout → stderr → non-zero-exit mapping.
- **Shared validators** (`validation.py`): `validate_space_id` (the `spaces/<id>` rule, previously duplicated across `chat.py` and `bootstrap.py`) and `validate_create_time` (a new RFC3339 guard for the Chat list `createTime` filter, validated before interpolation).
- **`messages.to_jsonl`**: single canonical JSON-line serializer for stdout/log output, built from the same envelope as `format_message` (replaces divergent `json.dumps(asdict(msg))` paths in the listen/serve loops so wire and log shapes cannot drift).
- **Config-driven `webhook_timeout`** (`CGC_WEBHOOK_TIMEOUT`, default `30.0`) and **`page_size`** (`CGC_PAGE_SIZE`, default `100`), replacing the previously hardcoded webhook HTTP timeout and Chat list page size. Documented in `docs/configuration.md`.
- **`bootstrap.SpaceNotFoundError`**: a configured-but-nonexistent/inaccessible space id (HTTP 404) now surfaces a distinct, actionable "space not found or app lacks access" error instead of the misleading Chat-app-configuration gate.
- **Terraform inputs** `subscription_ack_deadline_seconds`, `subscription_message_retention_duration`, and `chat_push_service_account` (with validation and documented defaults), replacing hardcoded Pub/Sub tunables and the publisher service-account literal. Documented in `terraform/README.md` and `terraform.tfvars.example`.

### Changed

- **Narrowed Chat-app not-configured classification** (`bootstrap.py`): the configuration gate (`ChatAppNotConfiguredError`) now fires on HTTP 403 `PERMISSION_DENIED` (and the explicit "is not configured" phrasing) only; a 404 on a configured space id maps to `SpaceNotFoundError` so operators get the correct remediation.
- **Truthful idempotent subscription result**: on HTTP 409 (subscription already exists), `bootstrap` now fetches and returns the real subscription resource name via `subscriptions.list` instead of a synthetic placeholder.
- **DRY refactors**: `chat.py` `list_messages`/`list_messages_as_app` share one pagination helper and one credentials-parameterized service builder; `chat.py`/`auth.py`/`bootstrap.py` route missing-value errors through `Config.require_keys` (single source of truth for the "set `<ENV>` …" hint); `cgc config set` routes through the shared `merge_config_values` validation (rejecting unknown keys up front with a clean non-zero exit); the CLI `_apply_overrides` helper centralizes the serve/listen/clear override pattern and `from dataclasses import replace` is hoisted to module scope.
- **Extracted `APP_MEMBER_NAME = "users/app"`** constant (with a comment noting it is the Chat API's fixed self-reference), replacing the inline literal.
- **`make lint` now also runs `ruff format --check`** (via the existing `format-check` target) so formatting drift is caught locally before push, matching the CI gate.
- **Removed prohibited suppressions** from the test suite (`# type: ignore[no-untyped-def]`, `# type: ignore[arg-type]`, `# pragma: no cover`) by fixing the root cause (typing the inner test function, using `typing.cast` for the deliberate invalid-input path, and refactoring the console-script lookup into a unit-tested helper).
- **Removed dead code**: the unused `complete_config_value` completer and its `_CONFIG_VALUE_KEYS` constant (never wired into the CLI) and their tests.
- **Pinned-action bump**: upgraded `actions/checkout@v4 -> @v5` and `astral-sh/setup-uv@v6 -> @v8` across the CI, release, and publish workflows to run on the current Node 24 runtime and clear the Node 20 deprecation warning.
- **CI** (`.github/workflows/ci.yml`) now includes a version-consistency gate (`pyproject.toml` vs `src/claude_google_chat/__init__.py:__version__`) and routes format checking through the `make format-check` target.
- **`docs/installation.md`** and **`docs/architecture.md`** document the build/release/publish pipeline, how to cut a release, how to finish PyPI setup (Trusted Publisher or API token), and the manual-publish path. `docs/installation.md` also notes the `cgc` console-command name overlap with an unrelated PyPI package and the unambiguous `python -m claude_google_chat` invocation.

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
