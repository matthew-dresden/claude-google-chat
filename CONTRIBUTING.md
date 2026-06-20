# Contributing to claude-google-chat

Thanks for your interest in contributing! This project is a Claude Code plugin plus a Python CLI (`cgc`). Contributions of all kinds are welcome: bug reports, docs, and code.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/matthew-dresden/claude-google-chat
cd claude-google-chat
make install        # uv sync --all-extras
```

Common tasks (wrappers around uv) are in the `Makefile`:

```bash
make lint           # uv run ruff check .
make format         # uv run ruff format .   (write mode — local only)
make typecheck      # uv run mypy src
make test           # uv run pytest
make build          # uv build
make all            # lint + typecheck + test + build
```

Run `make all` before opening a pull request. CI runs the same checks (using `ruff format --check` rather than the write-mode `format` target) across Python 3.11 and 3.12.

---

## Secret scanning (pre-commit)

This is a **public** repository, so secrets must never be committed. A
[gitleaks](https://github.com/gitleaks/gitleaks) pre-commit hook scans staged
changes locally before each commit. Install it once after cloning:

```bash
pip install pre-commit && pre-commit install
```

After `pre-commit install`, the hook runs automatically on every `git commit`
and blocks the commit if it detects a secret. To scan the whole tree on demand:

```bash
pre-commit run --all-files
```

The hook config lives in `.pre-commit-config.yaml` and the scan rules in
`.gitleaks.toml` (which extends the gitleaks defaults and allowlists the
project's known-safe placeholders and `tests/data/*.toml` fixtures).

**CI also enforces this.** The `gitleaks` GitHub Actions workflow
(`.github/workflows/gitleaks.yml`) runs the same scan on every push and pull
request using the gitleaks binary, so a missing local hook cannot let a secret
through. Do not bypass either check.

---

## Pull request guidelines

- **Keep changes focused.** One logical change per PR. Update docs in the **same** PR as the code they describe — documentation is part of "done", not a follow-up.
- **Update the changelog.** Add an entry to `CHANGELOG.md` under the `Unreleased` section in [Keep a Changelog](https://keepachangelog.com/) format.
- **Conventional commits.** Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (e.g. `feat: add delete command`, `fix: redact token in config show`, `docs: clarify listener timeout`).
- **No co-author trailers.** Do not add AI/assistant co-authorship trailers to commits or PRs.

---

## Quality gates — never bypass

- **Never use `--no-verify`** or any flag/annotation that skips hooks, linters, type checks, or security scans.
- **Never add suppression comments** (`# noqa`, `# nosec`, `# type: ignore`, etc.) to silence findings. If a check fails, fix the root cause. If you believe a finding is a false positive, open a discussion rather than suppressing it.
- **Never commit secrets.** No webhook URLs, OAuth client files, tokens, or `.env` files. Configuration lives in the OS config dir, not the repo. `token.json`, `.env`, and `*.local.*` are gitignored.

---

## Coding standards

- **SOLID and DRY.** `messages.py` is pure and I/O-free and is the single source of truth for the message envelope and protocol constants. `config.py` is the single config authority. Don't duplicate these.
- **Fail fast.** No silent fallbacks. Missing required config, network errors, and timeouts must exit non-zero with a clear, actionable message.
- **Env-driven, no hardcoding.** No hardcoded secrets, paths, timeouts, or endpoints. Everything comes from flags, environment variables, or the config file.
- **No `sleep` as a readiness primitive.** Waiting uses readiness/polling with env-driven cadence and a fail-fast idle timeout.
- **Idiomatic, typed Python.** Type annotations are required (`mypy` runs with `disallow_untyped_defs`). Format with `ruff format`; lint with `ruff`.

---

## Tests — real tests only

- Write **real** tests that can actually fail. No stub tests, no `assert True`, no placeholder assertions.
- Tests must be **input-driven**: load fixtures from `tests/data/`, set env via `monkeypatch`, and avoid hardcoded magic values in assertion paths.
- **No network in unit tests.** The required tests cover `config` and `messages`. Network paths (`auth`, `chat`, `listener`) are exercised with monkeypatched transport if added.
- Add or update tests for every behavior change.

Run the suite:

```bash
make test
```

---

## Reporting bugs and requesting features

Open an issue with:

- what you expected to happen,
- what actually happened,
- steps to reproduce (redact any secrets),
- your OS and Python version (`cgc --version`, `python --version`).

Thank you for contributing!
