"""Injectable probes and runner interfaces for onboarding + diagnostics.

The ``cgc doctor`` and ``cgc setup`` flows must reach into the host environment
(run ``gcloud``, perform the OAuth/ADC flow, send/read a real Chat message) — yet
their unit tests must never touch the network, ``gcloud``, real disk, or sleep.

This module is the **dependency-inversion seam** (DIP) for both flows: it defines
small, role-specific ``Protocol`` interfaces (ISP) for each external boundary
(running a command, probing the Chat API, polling readiness), a single
:class:`Probes` bundle that injects them, a production bundle built from real
collaborators, and the **pure** doctor check functions that consume a bundle.

Pure check functions take only data / injected probes and return a
:class:`CheckResult` ``{name, ok, fix}`` so the same logic is exercised by fast,
hermetic unit tests with fakes and by the real CLI with production probes.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from claude_google_chat.config import Config
from claude_google_chat.validation import validate_space_id

# OAuth scopes the Chat integration requires. ADC/OAuth must carry every Chat
# scope, plus the identity scopes needed to mint an ADC token; the doctor and
# setup both validate the token against this set. Single source of truth.
CHAT_SCOPE = "https://www.googleapis.com/auth/chat.messages"
IDENTITY_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)
# Scopes that an ADC login should request (Chat + identity).
ADC_LOGIN_SCOPES: tuple[str, ...] = (CHAT_SCOPE, *IDENTITY_SCOPES)
# Scopes that MUST be present on any token used for the Chat API. Identity scopes
# are requested for login but are not themselves required to call Chat, so the
# hard requirement is the Chat scope only — kept as a tuple for future growth.
REQUIRED_CHAT_SCOPES: tuple[str, ...] = (CHAT_SCOPE,)

# Well-formed incoming-webhook URL host + path shape. A webhook looks like
# ``https://chat.googleapis.com/v1/spaces/<id>/messages?key=...&token=...``.
WEBHOOK_HOST = "chat.googleapis.com"
WEBHOOK_PATH_SUFFIX = "/messages"

# Console deep links surfaced by setup's manual-fallback path. Centralised here
# (single source of truth) so the wizard and docs cannot drift.
CONSOLE_CREATE_PROJECT_URL = "https://console.cloud.google.com/projectcreate"
CONSOLE_ENABLE_CHAT_API_URL = "https://console.cloud.google.com/apis/library/chat.googleapis.com"
CONSOLE_OAUTH_CONSENT_URL = "https://console.cloud.google.com/apis/credentials/consent"
CONSOLE_OAUTH_CLIENT_URL = "https://console.cloud.google.com/apis/credentials/oauthclient"
GCLOUD_INSTALL_URL = "https://cloud.google.com/sdk/docs/install"


@dataclass(frozen=True)
class CommandResult:
    """Outcome of running an external command (e.g. ``gcloud``).

    Holds only what the probes need to decide success and surface a secret-free
    diagnostic: the exit code and captured streams. ``ok`` is the zero-exit
    convenience used by callers.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Return ``True`` when the command exited zero."""
        return self.returncode == 0


@runtime_checkable
class CommandRunner(Protocol):
    """Runs an external command and returns its :class:`CommandResult`.

    The injection seam for every ``gcloud`` call. Production uses a real
    subprocess runner; tests supply a fake that returns scripted results without
    spawning a process.
    """

    def run(self, args: Sequence[str]) -> CommandResult:
        """Run ``args`` (argv list) and return the captured result."""
        ...

    def which(self, program: str) -> str | None:
        """Return the resolved path to ``program`` on ``PATH``, or ``None``."""
        ...


@runtime_checkable
class ChatProbe(Protocol):
    """Probes the live Chat API with the resolved credentials (read/send round-trip).

    The injection seam for the network. Production talks to the real Chat API via
    the existing transport; tests supply a fake that records calls and returns
    scripted successes/failures so the round-trip gate is exercised offline.
    """

    def token_scopes(self, config: Config) -> list[str]:
        """Return the OAuth scopes carried by the resolved credentials."""
        ...

    def send_and_read_back(self, config: Config, marker: str) -> bool:
        """Send a test message containing ``marker`` and confirm it reads back.

        Returns ``True`` only when the round-trip succeeds (the marker is found
        on read-back), proving end-to-end send + read works.
        """
        ...


# A readiness predicate: returns ``True`` once the awaited condition holds.
ReadinessProbe = Callable[[], bool]


@dataclass(frozen=True)
class Probes:
    """Bundle of injected external collaborators for doctor + setup.

    Injected as one object (DIP) so a command receives all of its boundaries in a
    single, test-substitutable value. Production builds this via
    :func:`production_probes`; tests construct it directly with fakes.
    """

    runner: CommandRunner
    chat: ChatProbe
    env: Mapping[str, str]
    clock: Callable[[], float]
    sleeper: Callable[[float], None]


@dataclass(frozen=True)
class CheckResult:
    """Result of one doctor prerequisite check.

    Attributes:
        name: Human-readable check label (the RED/GREEN line text).
        ok: Whether the prerequisite is satisfied (GREEN) or not (RED).
        fix: The exact remediation command/text for a RED line; empty when ``ok``.
        required: Whether a RED result must fail ``cgc doctor`` (non-zero exit).
            A small number of checks are advisory (e.g. ADC vs OAuth-client is a
            choice) and do not by themselves fail the doctor.
    """

    name: str
    ok: bool
    fix: str = ""
    required: bool = True


# --------------------------------------------------------------------------- #
# Pure check functions. Each consumes injected probes / config and returns a
# CheckResult — no I/O of its own beyond the injected runner/chat probe.
# --------------------------------------------------------------------------- #


def check_gcloud_installed(probes: Probes) -> CheckResult:
    """GREEN when the ``gcloud`` CLI is resolvable on ``PATH``."""
    path = probes.runner.which("gcloud")
    if path:
        return CheckResult(name="gcloud CLI installed", ok=True)
    return CheckResult(
        name="gcloud CLI installed",
        ok=False,
        fix=f"install the Google Cloud CLI: {GCLOUD_INSTALL_URL}",
    )


def check_gcloud_logged_in(probes: Probes) -> CheckResult:
    """GREEN when an active gcloud account is reported by the runner.

    Runs ``gcloud auth list --filter=status:ACTIVE --format=value(account)``;
    non-empty stdout means a logged-in account.
    """
    result = probes.runner.run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]
    )
    if result.ok and result.stdout.strip():
        return CheckResult(name="gcloud account logged in", ok=True)
    return CheckResult(
        name="gcloud account logged in",
        ok=False,
        fix="log in to gcloud: run 'gcloud auth login'",
    )


def check_project_selected(probes: Probes) -> CheckResult:
    """GREEN when a gcloud project is configured (``core/project`` is set)."""
    result = probes.runner.run(["gcloud", "config", "get-value", "project"])
    project = result.stdout.strip()
    # gcloud prints the literal "(unset)" when no project is selected.
    if result.ok and project and project != "(unset)":
        return CheckResult(name=f"gcloud project selected ({project})", ok=True)
    return CheckResult(
        name="gcloud project selected",
        ok=False,
        fix="select a project: run 'cgc setup' (or 'gcloud config set project <PROJECT_ID>')",
    )


def check_chat_api_enabled(probes: Probes) -> CheckResult:
    """GREEN when the Google Chat API is enabled on the selected project.

    Runs ``gcloud services list --enabled`` filtered to ``chat.googleapis.com``;
    non-empty output means it is enabled.
    """
    result = probes.runner.run(
        [
            "gcloud",
            "services",
            "list",
            "--enabled",
            "--filter=config.name:chat.googleapis.com",
            "--format=value(config.name)",
        ]
    )
    if result.ok and "chat.googleapis.com" in result.stdout:
        return CheckResult(name="Google Chat API enabled", ok=True)
    return CheckResult(
        name="Google Chat API enabled",
        ok=False,
        fix="enable the Chat API: run 'cgc setup' "
        "(or 'gcloud services enable chat.googleapis.com')",
    )


def check_credentials_present(probes: Probes, config: Config) -> CheckResult:
    """GREEN when usable Chat credentials are present and valid.

    Delegates to the injected :class:`ChatProbe` to read the token's scopes; any
    failure (no token, unreadable, refused) is a RED with the re-auth fix.
    """
    try:
        probes.chat.token_scopes(config)
    except Exception as exc:
        from claude_google_chat.errors import map_error

        return CheckResult(
            name="OAuth/ADC credentials present & valid",
            ok=False,
            fix=f"{map_error(exc)} (run 'cgc setup')",
        )
    return CheckResult(name="OAuth/ADC credentials present & valid", ok=True)


def check_token_scopes(probes: Probes, config: Config) -> CheckResult:
    """GREEN when the token carries every required Chat scope.

    Guards silent scope-drop: a token that authenticates but lacks the Chat scope
    is RED with the exact missing scope named (via :func:`errors.format_missing_scopes`).
    """
    from claude_google_chat.errors import format_missing_scopes

    try:
        scopes = probes.chat.token_scopes(config)
    except Exception:
        # The credentials-present check already reports this RED with the fix;
        # avoid a duplicate, but still mark the scope check RED so a stale token
        # never silently passes the scope gate.
        return CheckResult(
            name="token has required Chat scopes",
            ok=False,
            fix="no readable credentials to check scopes on; run 'cgc setup'",
        )
    missing = format_missing_scopes(REQUIRED_CHAT_SCOPES, scopes)
    if missing:
        return CheckResult(name="token has required Chat scopes", ok=False, fix=missing)
    return CheckResult(name="token has required Chat scopes", ok=True)


def validate_webhook_url(url: str) -> str:
    """Return ``url`` if it is a well-formed incoming-webhook URL; else raise.

    A webhook is ``https://chat.googleapis.com/v1/spaces/<id>/messages?key=...&token=...``.
    The check is structural (host + path + the ``key``/``token`` query params) so
    a malformed value fails fast — it never echoes the token. Single source of
    truth used by the doctor check and setup's webhook prompt.
    """
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != WEBHOOK_HOST:
        raise ValueError(
            f"invalid webhook URL: expected an https URL on {WEBHOOK_HOST}; "
            "copy it from the Chat space's 'Manage webhooks' dialog"
        )
    if not parts.path.endswith(WEBHOOK_PATH_SUFFIX):
        raise ValueError(
            f"invalid webhook URL: the path must end with {WEBHOOK_PATH_SUFFIX!r}; "
            "copy the full incoming-webhook URL from the Chat space"
        )
    query = parse_qs(parts.query)
    missing = [param for param in ("key", "token") if not query.get(param)]
    if missing:
        named = " and ".join(missing)
        raise ValueError(
            f"invalid webhook URL: missing required {named} query parameter(s); "
            "copy the full incoming-webhook URL from the Chat space"
        )
    return url


def check_webhook_configured(config: Config) -> CheckResult:
    """GREEN when ``webhook_url`` is configured and well-formed (token never echoed)."""
    if not config.webhook_url:
        return CheckResult(
            name="webhook_url configured",
            ok=False,
            fix="set the incoming-webhook URL: run 'cgc setup' "
            "(or 'cgc config set webhook_url <URL>')",
        )
    try:
        validate_webhook_url(config.webhook_url)
    except ValueError as exc:
        return CheckResult(name="webhook_url well-formed", ok=False, fix=str(exc))
    return CheckResult(name="webhook_url configured & well-formed", ok=True)


def check_space_configured(config: Config) -> CheckResult:
    """GREEN when ``space_id`` is configured and matches ``spaces/<id>``."""
    if not config.space_id:
        return CheckResult(
            name="space_id configured",
            ok=False,
            fix="set the Chat space: run 'cgc setup' (or 'cgc config set space_id spaces/<id>')",
        )
    try:
        validate_space_id(config.space_id)
    except ValueError as exc:
        return CheckResult(name="space_id well-formed", ok=False, fix=str(exc))
    return CheckResult(name="space_id configured & well-formed", ok=True)


def check_config_file_present(config_path_exists: bool, config_path: str) -> CheckResult:
    """GREEN when the user config file exists.

    ``config_path_exists`` is injected (no disk I/O in the pure check) so tests
    drive both branches. A missing file is advisory — env-only configuration is
    valid — so this check does not by itself fail the doctor.
    """
    if config_path_exists:
        return CheckResult(name=f"config file present ({config_path})", ok=True)
    return CheckResult(
        name="config file present",
        ok=False,
        fix=f"create it: run 'cgc config init' (will write {config_path})",
        required=False,
    )


@dataclass(frozen=True)
class DoctorReport:
    """Aggregated doctor result: every check plus the overall pass/fail.

    ``ok`` is ``False`` when any **required** check failed, which the CLI maps to
    a non-zero exit.
    """

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return ``True`` only when every required check passed."""
        return all(check.ok for check in self.checks if check.required)


def run_all_checks(
    probes: Probes,
    config: Config,
    *,
    config_path_exists: bool,
    config_path: str,
) -> DoctorReport:
    """Run every prerequisite check in order and return the aggregated report.

    The single ordered list of checks consumed by ``cgc doctor`` and by tests.
    Each check is a pure function of the injected probes/config, so the whole
    report is reproducible offline.
    """
    checks = [
        check_gcloud_installed(probes),
        check_gcloud_logged_in(probes),
        check_project_selected(probes),
        check_chat_api_enabled(probes),
        check_credentials_present(probes, config),
        check_token_scopes(probes, config),
        check_webhook_configured(config),
        check_space_configured(config),
        check_config_file_present(config_path_exists, config_path),
    ]
    return DoctorReport(checks=checks)


# --------------------------------------------------------------------------- #
# Production collaborators (the only place real subprocess / network lives).
# --------------------------------------------------------------------------- #


class SubprocessRunner:
    """Production :class:`CommandRunner` backed by ``subprocess`` and ``shutil.which``.

    Captures stdout/stderr and never raises on a non-zero exit (the caller reads
    :attr:`CommandResult.returncode`); a missing executable surfaces as a
    non-zero result rather than an exception, so callers branch on data.
    """

    def run(self, args: Sequence[str]) -> CommandResult:
        try:
            # An argv list with no shell=True; inputs are fixed gcloud subcommands,
            # never user-interpolated shell strings.
            completed = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(returncode=127, stderr=str(exc))
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def which(self, program: str) -> str | None:
        return shutil.which(program)


class ChatApiProbe:
    """Production :class:`ChatProbe` over the real Chat transport + credentials.

    Reads scopes off the cached credentials and performs a real send + read-back
    round trip through the existing :mod:`claude_google_chat.chat` transport, so
    the same end-to-end gate the wizard advertises is exercised against the live
    API. Network failures propagate to the caller, which maps them via
    :func:`claude_google_chat.errors.map_error`.
    """

    def token_scopes(self, config: Config) -> list[str]:
        from claude_google_chat.auth import load_credentials

        creds = load_credentials(config)
        scopes = getattr(creds, "scopes", None)
        return list(scopes) if scopes else []

    def send_and_read_back(self, config: Config, marker: str) -> bool:
        from claude_google_chat.chat import list_messages, send_webhook
        from claude_google_chat.messages import ChatMessage

        send_webhook(config, ChatMessage(kind="status", status="info", text=marker))
        for raw in list_messages(config):
            if marker in raw.get("text", ""):
                return True
        return False


def production_probes(env: Mapping[str, str]) -> Probes:
    """Build the production :class:`Probes` bundle from real collaborators.

    The only constructor that wires the real subprocess runner, the live Chat
    probe, the process environment, and the real clock/sleeper. Injected at the
    CLI boundary so every layer below is test-substitutable.
    """
    import time

    return Probes(
        runner=SubprocessRunner(),
        chat=ChatApiProbe(),
        env=env,
        clock=time.monotonic,
        sleeper=time.sleep,
    )
