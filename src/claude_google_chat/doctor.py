"""``cgc doctor`` — RED/GREEN prerequisite checklist with the exact fix per red line.

Renders every onboarding prerequisite (gcloud installed / logged in / project
selected / Chat API enabled / credentials present & valid / token scopes /
webhook configured & well-formed / space configured / config file present) as a
coloured checklist. Each RED line carries the **exact** command that fixes it.
Also folds in the old trivial ``cgc setup`` behaviour by printing the config
file path.

The rendering is pure over a :class:`~claude_google_chat.probes.DoctorReport`
(itself built from pure, injectable checks), so the whole command is exercised
offline. The CLI wrapper exits non-zero when any *required* check fails (fail
fast), turning ``cgc doctor`` into a usable CI/health gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from claude_google_chat.config import Config, default_config_path
from claude_google_chat.probes import (
    CheckResult,
    DoctorReport,
    Probes,
    production_probes,
    run_all_checks,
)

# ANSI colour markers and the GREEN/RED glyphs. Kept here as the single source of
# truth for the doctor's presentation; colour is suppressed when not writing to a
# TTY so piped/CI output stays clean.
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"
_PASS_GLYPH = "PASS"
_FAIL_GLYPH = "FAIL"


def _colourize(text: str, colour: str, *, use_colour: bool) -> str:
    """Wrap ``text`` in ``colour`` when ``use_colour`` is set, else return as-is."""
    if not use_colour:
        return text
    return f"{colour}{text}{_RESET}"


def format_check_line(check: CheckResult, *, use_colour: bool) -> str:
    """Render one check as a ``[PASS]``/``[FAIL]`` line (with the fix when failed)."""
    if check.ok:
        marker = _colourize(f"[{_PASS_GLYPH}]", _GREEN, use_colour=use_colour)
        return f"{marker} {check.name}"
    marker = _colourize(f"[{_FAIL_GLYPH}]", _RED, use_colour=use_colour)
    suffix = "" if check.required else " (optional)"
    line = f"{marker} {check.name}{suffix}"
    if check.fix:
        line += f"\n        fix: {check.fix}"
    return line


def format_report(report: DoctorReport, config_path: str, *, use_colour: bool) -> str:
    """Render the full checklist plus the config-path footer and overall verdict."""
    lines = ["cgc doctor — prerequisite checklist", ""]
    for check in report.checks:
        lines.append(format_check_line(check, use_colour=use_colour))
    lines.append("")
    lines.append(f"config file: {config_path}")
    lines.append("")
    if report.ok:
        verdict = _colourize("all required checks passed", _GREEN, use_colour=use_colour)
    else:
        failed = [c.name for c in report.checks if c.required and not c.ok]
        named = ", ".join(failed)
        verdict = _colourize(
            f"required check(s) failed: {named} — fix the lines marked [{_FAIL_GLYPH}] above",
            _RED,
            use_colour=use_colour,
        )
    lines.append(verdict)
    return "\n".join(lines)


def build_report(
    probes: Probes,
    config: Config,
    config_path: str,
    *,
    config_path_exists: bool,
) -> DoctorReport:
    """Build the doctor report for ``config`` using ``probes`` (thin pure wrapper)."""
    return run_all_checks(
        probes,
        config,
        config_path_exists=config_path_exists,
        config_path=config_path,
    )


def run_doctor(
    *,
    env: Mapping[str, str],
    emit: Callable[[str], None],
    use_colour: bool,
    probes: Probes | None = None,
    config: Config | None = None,
    config_path: str | None = None,
    config_path_exists: bool | None = None,
) -> int:
    """Run every check, emit the rendered checklist, and return an exit code.

    Returns ``0`` when all required checks pass, ``1`` otherwise (fail fast). All
    boundaries are injectable: ``probes`` (gcloud/network), ``config`` and
    ``config_path``/``config_path_exists`` (filesystem), ``env`` (process env),
    and ``emit`` (output sink). Defaults wire the production probes and the real
    config so the CLI calls this with no fakes.
    """
    resolved_probes = probes if probes is not None else production_probes(env)
    resolved_config = config if config is not None else Config.load()
    path = config_path if config_path is not None else str(default_config_path())
    exists = (
        config_path_exists if config_path_exists is not None else default_config_path().exists()
    )
    report = build_report(
        resolved_probes,
        resolved_config,
        path,
        config_path_exists=exists,
    )
    emit(format_report(report, path, use_colour=use_colour))
    return 0 if report.ok else 1
