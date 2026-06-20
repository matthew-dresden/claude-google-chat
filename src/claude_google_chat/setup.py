"""``cgc setup`` — foolproof, idempotent, resumable onboarding wizard.

One command takes a fresh machine to a working two-way Google Chat integration.
Each step is **preflight-checked** (skip when already done), executed, then
**postflight-verified** (confirm it actually took effect), so re-running the
wizard fixes only the gaps and resumes after a step that previously failed.

Steps, in order:

1. **gcloud** — detect it; if missing, surface the install link and the manual
   console fallback (deep links to create a project + enable the API).
2. **Project** — create a new project or select an existing one.
3. **Chat API** — enable ``chat.googleapis.com`` and **poll for readiness**
   (not ``sleep``; a configurable timeout that fails fast) until it reports
   ENABLED.
4. **Auth (ADC first)** — try ``gcloud auth application-default login`` with the
   Chat + identity scopes and probe whether the Chat API accepts that ADC token;
   if it does, use ADC. Otherwise fall back to a guided OAuth-client flow
   (console deep links + client-secrets shape validation + ``cgc auth login``).
   After **either** path, verify the token carries every required Chat scope and
   re-auth if not (guards silent scope-drop).
5. **Webhook** — prompt for / validate the incoming-webhook URL shape; store it;
   never echo the token.
6. **Verify end-to-end** — a real send + read-back round trip before declaring
   success. On any failure, print the mapped actionable error and which step to
   re-run.

Every gcloud / auth / network call goes through the injected
:class:`~claude_google_chat.probes.Probes` runners, so tests drive the whole
wizard with fakes — no network, gcloud, real disk, or sleep.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from claude_google_chat.config import Config, default_config_path, merge_and_write_config
from claude_google_chat.errors import format_missing_scopes, map_error
from claude_google_chat.probes import (
    ADC_LOGIN_SCOPES,
    CONSOLE_CREATE_PROJECT_URL,
    CONSOLE_ENABLE_CHAT_API_URL,
    CONSOLE_OAUTH_CLIENT_URL,
    CONSOLE_OAUTH_CONSENT_URL,
    GCLOUD_INSTALL_URL,
    REQUIRED_CHAT_SCOPES,
    Probes,
    check_chat_api_enabled,
    check_project_selected,
    production_probes,
    validate_webhook_url,
)

# Env-driven readiness timeout for the Chat-API-enable poll. No hardcoded value:
# read from the environment (injectable mapping) with a documented default, and a
# fail-fast diagnostic when exceeded.
ENABLE_TIMEOUT_ENV = "CGC_SETUP_ENABLE_TIMEOUT"
ENABLE_POLL_INTERVAL_ENV = "CGC_SETUP_ENABLE_POLL_INTERVAL"
DEFAULT_ENABLE_TIMEOUT = 120.0
DEFAULT_ENABLE_POLL_INTERVAL = 3.0

# Prefix for the end-to-end round-trip test message (a unique marker is appended
# so the read-back gate matches exactly our own message).
ROUNDTRIP_MARKER_PREFIX = "cgc-setup-verify"


class SetupError(RuntimeError):
    """Raised when a setup step fails after exhausting its remediation.

    Carries an already-mapped, actionable, secret-free message naming which step
    to re-run, so the CLI surfaces it directly (fail fast, non-zero exit).
    """


@runtime_checkable
class SetupIO(Protocol):
    """Injected user-interaction + output channel for the wizard (DIP seam).

    A structural protocol so the wizard never depends on ``typer`` directly:
    the CLI supplies :class:`TyperSetupIO` (real prompts/echo) and tests supply a
    scripted double — anything with ``prompt``/``emit``/``confirm`` satisfies it.
    """

    def prompt(self, message: str) -> str:
        """Prompt the user for free-text input and return the answer."""
        ...

    def emit(self, line: str) -> None:
        """Emit one output line to the user."""
        ...

    def confirm(self, message: str) -> bool:
        """Ask the user a yes/no question and return their choice."""
        ...


@dataclass(frozen=True)
class TyperSetupIO:
    """Concrete :class:`SetupIO` over injectable prompt/emit/confirm callables.

    The production adapter the CLI builds from ``typer.prompt``/``echo``/
    ``confirm``; kept as a thin callable-backed value so the wizard stays
    decoupled from any specific UI library.
    """

    _prompt: Callable[[str], str]
    _emit: Callable[[str], None]
    _confirm: Callable[[str], bool]

    def prompt(self, message: str) -> str:
        return self._prompt(message)

    def emit(self, line: str) -> None:
        self._emit(line)

    def confirm(self, message: str) -> bool:
        return self._confirm(message)


@dataclass(frozen=True)
class SetupOptions:
    """Flags controlling a setup run.

    Attributes:
        reauth: Only redo authentication (skip project/API/webhook steps).
        dry_run: Show the actions that would run, change nothing.
        verify: Only run the end-to-end round-trip check.
    """

    reauth: bool = False
    dry_run: bool = False
    verify: bool = False


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    """Return ``env[name]`` parsed as a float, or ``default``; fail fast on garbage."""
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SetupError(f"invalid {name}={raw!r}; expected a number of seconds") from exc


# --------------------------------------------------------------------------- #
# Readiness polling (no sleep-as-synchronisation; active probe + fail-fast).
# --------------------------------------------------------------------------- #


def poll_until_ready(
    *,
    is_ready: Callable[[], bool],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    timeout: float,
    interval: float,
    timeout_message: str,
) -> None:
    """Poll ``is_ready`` on ``interval`` until true or ``timeout`` elapses.

    Active readiness detection (not a fixed ``sleep``): each iteration calls the
    injected predicate; the cadence sleeper paces polling and is injectable so
    tests advance a fake clock without real waiting. On timeout this raises
    :class:`SetupError` with ``timeout_message`` (fail fast, non-zero exit).
    """
    start = clock()
    while True:
        if is_ready():
            return
        if (clock() - start) >= timeout:
            raise SetupError(timeout_message)
        sleeper(interval)


# --------------------------------------------------------------------------- #
# Step: gcloud detection.
# --------------------------------------------------------------------------- #


def step_detect_gcloud(probes: Probes, io: SetupIO) -> bool:
    """Return ``True`` when gcloud is available; print the manual fallback if not.

    A missing gcloud is not fatal on its own: the wizard surfaces the install
    link plus the console deep links so a user can complete project + API setup
    manually, then returns ``False`` so the caller skips the gcloud-driven steps.
    """
    if probes.runner.which("gcloud"):
        io.emit("[ok] gcloud CLI detected")
        return True
    io.emit("[!] gcloud CLI not found.")
    io.emit(f"    Install it: {GCLOUD_INSTALL_URL}")
    io.emit("    Or set up manually in the console:")
    io.emit(f"      1. Create a project:   {CONSOLE_CREATE_PROJECT_URL}")
    io.emit(f"      2. Enable the Chat API: {CONSOLE_ENABLE_CHAT_API_URL}")
    return False


# --------------------------------------------------------------------------- #
# Step: project create/select.
# --------------------------------------------------------------------------- #


def step_project(probes: Probes, io: SetupIO, *, dry_run: bool) -> None:
    """Ensure a gcloud project is selected (preflight skip / create / select)."""
    if check_project_selected(probes).ok:
        io.emit("[ok] gcloud project already selected")
        return
    if dry_run:
        io.emit("[dry-run] would create or select a gcloud project")
        return
    create = io.confirm("Create a NEW Google Cloud project? (No = select an existing one)")
    if create:
        project_id = io.prompt("New project id").strip()
        if not project_id:
            raise SetupError("a project id is required to create a project; re-run 'cgc setup'")
        result = probes.runner.run(["gcloud", "projects", "create", project_id])
        if not result.ok:
            raise SetupError(
                f"failed to create project {project_id!r}; "
                "verify the id is globally unique and you have permission, then re-run 'cgc setup'"
            )
        probes.runner.run(["gcloud", "config", "set", "project", project_id])
    else:
        project_id = io.prompt("Existing project id to use").strip()
        if not project_id:
            raise SetupError("a project id is required; re-run 'cgc setup'")
        result = probes.runner.run(["gcloud", "config", "set", "project", project_id])
        if not result.ok:
            raise SetupError(f"failed to select project {project_id!r}; re-run 'cgc setup'")
    # Postflight: confirm a project is now selected.
    if not check_project_selected(probes).ok:
        raise SetupError("project selection did not take effect; re-run 'cgc setup' (project step)")
    io.emit(f"[ok] project set to {project_id}")


# --------------------------------------------------------------------------- #
# Step: enable Chat API + poll for readiness.
# --------------------------------------------------------------------------- #


def step_enable_chat_api(probes: Probes, io: SetupIO, *, dry_run: bool) -> None:
    """Enable the Chat API and poll until it reports ENABLED (readiness, not sleep)."""
    if check_chat_api_enabled(probes).ok:
        io.emit("[ok] Google Chat API already enabled")
        return
    if dry_run:
        io.emit("[dry-run] would enable chat.googleapis.com and poll until ENABLED")
        return
    result = probes.runner.run(["gcloud", "services", "enable", "chat.googleapis.com"])
    if not result.ok:
        raise SetupError(
            "failed to enable chat.googleapis.com; ensure billing/permissions are set, "
            "then re-run 'cgc setup'"
        )
    timeout = _float_env(probes.env, ENABLE_TIMEOUT_ENV, DEFAULT_ENABLE_TIMEOUT)
    interval = _float_env(probes.env, ENABLE_POLL_INTERVAL_ENV, DEFAULT_ENABLE_POLL_INTERVAL)
    poll_until_ready(
        is_ready=lambda: check_chat_api_enabled(probes).ok,
        clock=probes.clock,
        sleeper=probes.sleeper,
        timeout=timeout,
        interval=interval,
        timeout_message=(
            f"Google Chat API did not report ENABLED within {timeout}s "
            f"({ENABLE_TIMEOUT_ENV}); check the project in the console "
            f"({CONSOLE_ENABLE_CHAT_API_URL}) and re-run 'cgc setup'"
        ),
    )
    io.emit("[ok] Google Chat API enabled")


# --------------------------------------------------------------------------- #
# Step: auth (ADC first, OAuth-client fallback) + scope-drop guard.
# --------------------------------------------------------------------------- #


def _token_scopes_or_empty(probes: Probes, config: Config) -> list[str]:
    """Return the token's scopes, or ``[]`` when no usable credentials exist."""
    try:
        return probes.chat.token_scopes(config)
    except Exception:
        return []


def _verify_scopes(probes: Probes, config: Config) -> None:
    """Raise :class:`SetupError` when the token is missing a required Chat scope.

    The silent-scope-drop guard run after **either** auth path: a token can
    authenticate yet lack the Chat scope (e.g. an org consent narrowed it), which
    would fail later in a confusing way. We name the missing scope and the
    re-auth command up front (DRY via :func:`errors.format_missing_scopes`).
    """
    scopes = _token_scopes_or_empty(probes, config)
    missing = format_missing_scopes(REQUIRED_CHAT_SCOPES, scopes)
    if missing:
        raise SetupError(missing)


def _try_adc(probes: Probes, io: SetupIO, config: Config) -> bool:
    """Attempt ADC login + Chat-acceptance probe. Return ``True`` if ADC works.

    Runs ``gcloud auth application-default login`` with the Chat + identity
    scopes (no OAuth client to create), then probes whether the Chat API accepts
    that ADC token by reading its scopes. Returns ``False`` (not fatal) so the
    caller falls back to the guided OAuth-client path.
    """
    scope_arg = "--scopes=" + ",".join(ADC_LOGIN_SCOPES)
    result = probes.runner.run(["gcloud", "auth", "application-default", "login", scope_arg])
    if not result.ok:
        io.emit("[!] ADC login did not complete; falling back to an OAuth client")
        return False
    scopes = _token_scopes_or_empty(probes, config)
    if not scopes:
        io.emit("[!] the Chat API did not accept the ADC token; falling back to an OAuth client")
        return False
    io.emit("[ok] authenticated via Application Default Credentials")
    return True


def _guided_oauth_client(probes: Probes, io: SetupIO, config: Config) -> Config:
    """Run the guided OAuth-client fallback: deep links + shape check + login.

    Prints the exact console deep links to create a Desktop OAuth client and
    consent screen, prompts for the downloaded client-secrets JSON path,
    validates its shape (a Desktop/installed client), stores it, and runs the
    cached-token login through the injected runner-free :func:`auth.login`
    indirection. Returns the updated config carrying ``oauth_client_file``.
    """
    io.emit("Set up an OAuth client (fallback path):")
    io.emit(f"    1. Configure the consent screen: {CONSOLE_OAUTH_CONSENT_URL}")
    io.emit(f"    2. Create a Desktop OAuth client: {CONSOLE_OAUTH_CLIENT_URL}")
    client_file = io.prompt("Path to the downloaded client-secrets JSON").strip()
    if not client_file:
        raise SetupError("a client-secrets JSON path is required; re-run 'cgc setup --reauth'")
    validate_client_secrets(client_file)
    written = merge_and_write_config({"oauth_client_file": client_file})
    io.emit(f"[ok] stored oauth_client_file in {written}")
    config = Config.load()
    _run_oauth_login(config)
    io.emit("[ok] OAuth token cached")
    return config


def validate_client_secrets(path: str) -> None:
    """Validate that ``path`` is a Desktop/installed OAuth client-secrets JSON.

    Reads the file and checks the top-level ``installed`` key and the required
    fields Google emits for a Desktop client (``client_id``, ``client_secret``,
    ``auth_uri``, ``token_uri``). Fails fast with an actionable message — and
    never echoes the secret values — so a wrong file (e.g. a Web client or a
    service-account key) is caught before the OAuth flow starts.
    """
    import json
    from pathlib import Path

    file_path = Path(path)
    if not file_path.exists():
        raise SetupError(f"client-secrets file not found: {path}; re-run 'cgc setup --reauth'")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SetupError(
            f"client-secrets file is not valid JSON: {path}; "
            "download a fresh Desktop OAuth client and re-run 'cgc setup --reauth'"
        ) from exc
    if not isinstance(data, dict) or "installed" not in data:
        raise SetupError(
            "client-secrets file is not a Desktop OAuth client (missing top-level "
            "'installed' key); create an 'OAuth client ID' of type 'Desktop app' and "
            "re-run 'cgc setup --reauth'"
        )
    section = data["installed"]
    required = ("client_id", "client_secret", "auth_uri", "token_uri")
    if not isinstance(section, dict):
        raise SetupError(
            "client-secrets 'installed' section is malformed; re-download the Desktop OAuth "
            "client and re-run 'cgc setup --reauth'"
        )
    missing = [field for field in required if not section.get(field)]
    if missing:
        named = ", ".join(missing)
        raise SetupError(
            f"client-secrets file is missing required field(s): {named}; re-download the "
            "Desktop OAuth client and re-run 'cgc setup --reauth'"
        )


def _run_oauth_login(config: Config) -> None:
    """Run the installed-app OAuth flow (indirection kept thin for substitution)."""
    from claude_google_chat.auth import login

    login(config)


def step_auth(probes: Probes, io: SetupIO, config: Config, *, dry_run: bool) -> Config:
    """Authenticate ADC-first, fall back to OAuth client, then verify scopes.

    Idempotent: when the current credentials already carry every required Chat
    scope, this is a no-op (re-running setup does not re-auth needlessly). After
    obtaining credentials by either path, the scope-drop guard runs so a token
    that authenticates without the Chat scope fails fast with the missing scope
    named.
    """
    if not dry_run:
        existing = _token_scopes_or_empty(probes, config)
        if existing and not format_missing_scopes(REQUIRED_CHAT_SCOPES, existing):
            io.emit("[ok] existing credentials already carry the required Chat scopes")
            return config
    if dry_run:
        io.emit("[dry-run] would authenticate (ADC first, OAuth-client fallback)")
        return config

    if not _try_adc(probes, io, config):
        config = _guided_oauth_client(probes, io, config)
    _verify_scopes(probes, config)
    io.emit("[ok] credentials carry the required Chat scopes")
    return config


# --------------------------------------------------------------------------- #
# Step: webhook prompt + validation + store (token never echoed).
# --------------------------------------------------------------------------- #


def step_webhook(io: SetupIO, config: Config, *, dry_run: bool) -> Config:
    """Ensure a well-formed ``webhook_url`` is configured (token never echoed)."""
    if config.webhook_url:
        try:
            validate_webhook_url(config.webhook_url)
        except ValueError:
            pass
        else:
            io.emit("[ok] webhook_url already configured")
            return config
    if dry_run:
        io.emit("[dry-run] would prompt for and store the incoming-webhook URL")
        return config
    url = io.prompt("Incoming webhook URL (from the Chat space 'Manage webhooks')").strip()
    try:
        validate_webhook_url(url)
    except ValueError as exc:
        # The validator's message never includes the token; re-surface it as-is.
        raise SetupError(f"{exc}; re-run 'cgc setup'") from exc
    written = merge_and_write_config({"webhook_url": url})
    # Never echo the URL (it carries the token); report only the file written.
    io.emit(f"[ok] stored webhook_url in {written}")
    return Config.load()


# --------------------------------------------------------------------------- #
# Step: end-to-end round-trip gate.
# --------------------------------------------------------------------------- #


def step_verify_roundtrip(probes: Probes, io: SetupIO, config: Config) -> None:
    """Send a unique test message and read it back; fail fast if it does not arrive.

    The end-to-end gate run before declaring success: it proves outbound (webhook
    send) and inbound (Chat read) both work with the resolved credentials. A
    unique marker is used so the read-back matches exactly our own message. Any
    transport failure is mapped to an actionable message (no traceback) naming
    which step to re-run.
    """
    marker = f"{ROUNDTRIP_MARKER_PREFIX}-{secrets.token_hex(8)}"
    try:
        ok = probes.chat.send_and_read_back(config, marker)
    except Exception as exc:
        raise SetupError(
            f"end-to-end verification failed: {map_error(exc)} "
            "(re-run 'cgc setup --verify' after fixing)"
        ) from exc
    if not ok:
        raise SetupError(
            "end-to-end verification failed: the test message was sent but did not read back; "
            "confirm the webhook and space point at the SAME space, then re-run "
            "'cgc setup --verify'"
        )
    io.emit("[ok] end-to-end round-trip succeeded (sent and read back a test message)")


# --------------------------------------------------------------------------- #
# Orchestrator.
# --------------------------------------------------------------------------- #


def run_setup(
    *,
    io: SetupIO,
    options: SetupOptions,
    env: Mapping[str, str],
    probes: Probes | None = None,
    config: Config | None = None,
) -> int:
    """Drive the wizard end to end, returning a process exit code.

    Returns ``0`` on success, non-zero on any fail-fast :class:`SetupError`
    (whose already-mapped, actionable message is emitted). Honours the flags:
    ``--verify`` runs only the round-trip gate; ``--reauth`` runs only the auth
    step (+ its scope guard); ``--dry-run`` shows actions and changes nothing.
    All boundaries are injectable; defaults wire the production probes + config.
    """
    resolved_probes = probes if probes is not None else production_probes(env)
    resolved_config = config if config is not None else Config.load()

    try:
        return _run_steps(resolved_probes, resolved_config, io, options)
    except SetupError as exc:
        io.emit(f"[fail] {exc}")
        return 1


def _run_steps(
    probes: Probes,
    config: Config,
    io: SetupIO,
    options: SetupOptions,
) -> int:
    """Execute the selected steps in order (separated for a single try boundary)."""
    if options.verify:
        step_verify_roundtrip(probes, io, config)
        io.emit("setup verification complete.")
        return 0

    if options.reauth:
        step_auth(probes, io, config, dry_run=options.dry_run)
        if not options.dry_run:
            io.emit("re-authentication complete.")
        else:
            io.emit("[dry-run] no changes made.")
        return 0

    has_gcloud = step_detect_gcloud(probes, io)
    if has_gcloud:
        step_project(probes, io, dry_run=options.dry_run)
        step_enable_chat_api(probes, io, dry_run=options.dry_run)
    config = step_auth(probes, io, config, dry_run=options.dry_run)
    config = step_webhook(io, config, dry_run=options.dry_run)

    if options.dry_run:
        io.emit("[dry-run] no changes made; re-run without --dry-run to apply.")
        return 0

    step_verify_roundtrip(probes, io, config)
    io.emit("")
    io.emit(f"setup complete. Config: {default_config_path()}")
    io.emit("Run 'cgc doctor' any time to re-check prerequisites.")
    return 0
