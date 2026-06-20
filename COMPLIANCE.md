# CLAUDE.md Compliance

This document records how `claude-google-chat` meets the engineering and
automation standards in the repository's `CLAUDE.md`, the justified exceptions,
and any follow-ups. It is a point-in-time summary; the code, tests, and CI gates
are the authority.

Verification at time of writing — all gates pass:

- `make lint` (now runs `ruff check` **and** `ruff format --check`)
- `make typecheck` (`mypy src`)
- `make test` (pytest, coverage gate `--cov-fail-under=90`; actual 100%)
- `make build` (`uv build`)
- `gitleaks detect --source . --redact --exit-code 1` (no leaks)

---

## Core principles

### Never assume success — always verify
All changes in this pass were verified by running the full gate above. Helpers
that previously assumed a non-`None` value now go through `Config.require_keys`,
which fails fast and names the missing key.

### Evidence-based communication
No speculative percentages or timing claims are made in code, comments, or docs.
Refactors are described qualitatively (e.g. "shared poll primitive", "config
-driven timeout"), never as quantified speed/memory gains.

### Fail-fast philosophy
- No fallback logic was introduced. Idempotent API paths (HTTP 409) return a
  *truthful* value (the real subscription resource name, fetched via
  `subscriptions.list`) rather than a fabricated placeholder.
- No hardcoded configuration: webhook HTTP timeout (`webhook_timeout` /
  `CGC_WEBHOOK_TIMEOUT`) and Chat list page size (`page_size` / `CGC_PAGE_SIZE`)
  are now `Config` fields with documented non-secret defaults. Terraform
  Pub/Sub tunables (`ack_deadline_seconds`, `message_retention_duration`) and the
  Chat push publisher account are now input variables.
- No silent failures: every error path raises with an actionable, non-secret
  message and a non-zero exit code.

### SOLID / DRY
- **Shared poll primitive** (`polling.py::PollLoop`): the duplicated
  `_seen`/`_since` dedup, high-water `createTime` tracking, idle-timeout run loop,
  and one-JSON-line-per-message stdout emit that previously lived in both
  `listener.py` and `serve.py` now live once. Each caller supplies only its
  per-message predicate/action. `run_to_exit_code` removes the duplicated
  try/except-timeout → stderr → return-1 wrappers.
- **Shared pagination + service build** (`chat.py`): `list_messages` and
  `list_messages_as_app` delegate to one private `_list_messages` paginator;
  `_build_service` and `build_app_service` delegate to one
  `_build_chat_service` parameterized by the credentials loader.
- **Shared validators** (`validation.py`): the `spaces/<id>` regex/error and a
  new RFC3339 `createTime` guard are defined once and imported by `chat.py` and
  `bootstrap.py` (was duplicated across three call sites).
- **Single JSON serializer** (`messages.py::to_jsonl`): stdout/log emission now
  routes through the same envelope builder as `format_message`, so the wire and
  log shapes cannot drift as `ChatMessage` fields change (replaced `asdict()`).
- **Single missing-config message**: `chat.py`, `auth.py`, and `bootstrap.py`
  now call `Config.require_keys` instead of hand-building "missing required
  config value … (set <ENV> …)" strings, so the wording/env-hint has one source
  of truth (`ENV_OVERRIDES`).
- **Single config-set validation**: `cgc config set` routes through
  `merge_config_values` (via `merge_and_write_config`), the same validator used
  by `cgc bootstrap`, rejecting unknown keys up front with a clean non-zero exit.
- **CLI override helper** (`_apply_overrides`): the repeated
  "replace-the-field-only-if-the-flag-was-given" pattern in `serve`/`listen`/
  `clear` is centralized; `from dataclasses import replace` is hoisted to module
  top-level.

### Complete replacement of superseded code
- The dead `complete_config_value` completer and its `_CONFIG_VALUE_KEYS`
  constant (used only by their own tests, never wired into the CLI) were removed
  along with their tests. A repo-wide grep confirms zero remaining references.
- Every test affected by a behavior change (narrowed not-configured
  classification, real subscription name on 409, config-set validation path,
  added numeric env overrides) was updated to assert the new correct behavior —
  no test was patched to work around removed code, and no suppression was added.

### 12-factor / idiomatic / declarative / environment-agnostic / immutable
- Config from environment/file; secrets have no defaults and are never echoed.
- Logs are unbuffered single-JSON-line stdout writes.
- Terraform stays declarative and input-driven; promoting the remaining
  hardcoded literals to variables preserves "build once / configure per tenant".

### Input-driven / no hardcoded values
- New env-overridable knobs: `CGC_WEBHOOK_TIMEOUT`, `CGC_PAGE_SIZE`.
- Terraform inputs added: `subscription_ack_deadline_seconds`,
  `subscription_message_retention_duration`, `chat_push_service_account`.
- `APP_MEMBER_NAME = "users/app"` is extracted to a named, commented constant
  documenting it is the Chat API's fixed self-reference (an API contract value,
  not an arbitrary id).

---

## Security standards

- **No suppressions**: the three prohibited annotations that previously existed
  in tests (`# type: ignore[no-untyped-def]`, `# type: ignore[arg-type]`,
  `# pragma: no cover`) were removed by fixing the root cause (typing the inner
  test function, using `typing.cast` for the deliberate invalid-input path, and
  refactoring the console-script lookup into a unit-tested helper). A repo-wide
  grep for `noqa|nosec|type: ignore|pragma: no cover|nolint|SuppressWarnings`
  returns nothing.
- **Input validation / safe external calls**: the Chat API `createTime` list
  filter value is now validated against an RFC3339 shape before interpolation,
  so a malformed/unexpected timestamp fails fast instead of being injected
  verbatim into the API `filter` expression.
- **Accurate, non-leaky errors**: a mistyped/inaccessible space id (HTTP 404)
  now raises a distinct `SpaceNotFoundError` with the correct remediation,
  instead of being misreported with the long "configure your Chat app" gate
  instructions. The configuration gate (`ChatAppNotConfiguredError`) is reserved
  for the true signal (HTTP 403 / explicit "is not configured" phrasing).
- Secret scanning runs in CI and as a pre-commit hook; `gitleaks detect` passes.

---

## Waiting and readiness detection

No `sleep`-based readiness waits were added. The poll cadence (`poll_interval`)
and idle `listen_timeout` are env-driven; an exceeded idle timeout fails fast
with a clear diagnostic and a non-zero exit. The injectable `sleeper` paces
polling only (a documented cadence, not a readiness wait) — see the justified
exception below.

---

## Shell / scripting / GitHub workflows / git usage

- No shell scripts were created. CI `run` steps use `shell: bash` with
  `set -euo pipefail` (unchanged).
- The `lint` Make target now also runs `ruff format --check` (via a dependency
  on the existing `format-check` target), so a formatting drift is caught locally
  by `make lint` before push, matching the CI gate. No hook/linter/scanner was
  bypassed.

---

## Documentation synchronization

Docs were updated in lockstep with the code:
- `docs/configuration.md`: documents `webhook_timeout` / `page_size` (reference
  table + the timeouts/cadence section).
- `docs/architecture.md`: adds `polling.py` and `validation.py`, notes the shared
  `PollLoop`, the config-driven timeout/page-size, `SpaceNotFoundError`, and that
  the `Stop` hook requires `webhook_url`.
- `terraform/README.md` + `terraform/terraform.tfvars.example`: document the new
  Terraform inputs and the tfvars-based override for the publisher SA.

---

## Justified exceptions

1. **Finance/compliance regulatory controls (SOC 2 / PCI DSS / FINRA / SEC /
   GDPR / CCPA / SOX), container/Kubernetes hardening, JWT/session/crypto/API
   gateway controls** — **N/A**. This project is a local developer CLI + Terraform
   module + Claude Code plugin. It handles no payment, PII, or financial data,
   exposes no network service or web surface, issues no tokens, and ships no
   container/K8s manifests. The applicable security controls that *do* apply
   (secret handling, input validation, no-suppression, fail-fast, least-privilege
   IAM in Terraform) are met.

2. **Completer error-swallowing** (`completion.py::safe_completer`) — **justified,
   left as-is**. A Typer/Click completion callback runs inside the user's
   interactive shell on every `<TAB>`; if it raised, the shell would print a
   traceback. Returning `[]` on any error is the correct, intended behavior for a
   shell-completion side-effect (analogous to the completion-callback exemption in
   the standards), not a swallowed application error. It is documented in the
   decorator's docstring and covered by tests.

3. **`Stop` hook fail-fast before setup** (`hooks/hooks.json`) — **justified, no
   code change**. The hook runs `cgc chat send`, which correctly fails fast when
   `webhook_url` is unconfigured. Rather than weaken that fail-fast, the
   requirement is **documented** (`docs/architecture.md`): configure `webhook_url`
   before relying on the hook, or remove the hook until setup is complete.

4. **Injectable `sleeper` poll cadence** (`listener.py` / `serve.py` /
   `polling.py`) — **justified**. This is a documented, env-driven polling cadence
   (`poll_interval`), not a `sleep`-based readiness wait. Readiness is handled by
   the fail-fast idle timeout. The sleeper is injectable so tests never sleep.

5. **Terraform `region` variable "unused"** (candidate finding) — **false
   positive**. `var.region` *is* referenced: it configures both the `google` and
   `google-beta` providers in `terraform/versions.tf` (the finding only inspected
   `main.tf`/`outputs.tf`). The variable is kept; no change needed.

6. **Pre-existing `mypy` findings in `tests/test_completion_functional.py`**
   (`shutil.which(...) -> str | None` passed into a subprocess arg list) — **out
   of scope, pre-existing**. These are not introduced by this pass, are unrelated
   to the candidate findings, and are outside the CI type gate (`mypy src`). Left
   untouched to avoid scope creep; flagged here as a follow-up.

---

## Follow-ups

- Consider widening the `mypy` gate to `tests/` and resolving the three
  pre-existing `str | None` subprocess-arg findings in
  `tests/test_completion_functional.py`.
- Optional: thread `page_size` through as a CLI flag if operators ever need to
  tune it per-invocation (currently env/file-driven, which satisfies the
  input-driven standard).
