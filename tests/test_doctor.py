"""Tests for ``cgc doctor`` rendering + exit-code semantics (:mod:`...doctor`).

Drive ``run_doctor`` with fully injected probes/config so the RED/GREEN
checklist, the per-red fix lines, the config-path footer, and the non-zero exit
on a required failure are all asserted offline — no gcloud, network, or real disk.
"""

from __future__ import annotations

from collections.abc import Callable

from claude_google_chat.config import Config
from claude_google_chat.doctor import format_check_line, format_report, run_doctor
from claude_google_chat.probes import (
    CHAT_SCOPE,
    CheckResult,
    CommandResult,
    DoctorReport,
)
from tests.fakes import FakeChatProbe, FakeCommandRunner, make_probes

WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=SECRETKEY&token=SECRETTOKEN"
SPACE_ID = "spaces/AAAA"


def _all_green_runner() -> FakeCommandRunner:
    return FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": CommandResult(0, stdout="chat.googleapis.com"),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )


def test_format_check_line_pass_and_fail() -> None:
    green = format_check_line(CheckResult(name="x", ok=True), use_colour=False)
    assert green.startswith("[PASS]")
    red = format_check_line(
        CheckResult(name="y", ok=False, fix="run 'cgc setup'"), use_colour=False
    )
    assert red.startswith("[FAIL]")
    assert "fix: run 'cgc setup'" in red


def test_format_check_line_colour_wraps_marker() -> None:
    green = format_check_line(CheckResult(name="x", ok=True), use_colour=True)
    assert "\033[32m" in green


def test_format_report_lists_config_path_and_verdict() -> None:
    report = DoctorReport(checks=[CheckResult(name="ok-check", ok=True)])
    text = format_report(report, "/cfg/config.toml", use_colour=False)
    assert "config file: /cfg/config.toml" in text
    assert "all required checks passed" in text


def test_format_report_failed_verdict_names_failing_checks() -> None:
    report = DoctorReport(
        checks=[CheckResult(name="bad-check", ok=False, fix="do x", required=True)]
    )
    text = format_report(report, "/cfg/config.toml", use_colour=False)
    assert "bad-check" in text
    assert "fix the lines marked" in text


def test_run_doctor_all_green_exits_zero(make_config: Callable[..., Config]) -> None:
    lines: list[str] = []
    probes = make_probes(runner=_all_green_runner(), chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    config = make_config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    code = run_doctor(
        env={},
        emit=lines.append,
        use_colour=False,
        probes=probes,
        config=config,
        config_path="/cfg/config.toml",
        config_path_exists=True,
    )
    assert code == 0
    text = "\n".join(lines)
    assert "all required checks passed" in text
    assert "[FAIL]" not in text


def test_run_doctor_red_required_exits_nonzero_with_fixes(
    make_config: Callable[..., Config],
) -> None:
    lines: list[str] = []
    runner = FakeCommandRunner(which_map={"gcloud": None})  # gcloud missing
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=FileNotFoundError("x")))
    config = make_config(webhook_url=None, space_id=None)
    code = run_doctor(
        env={},
        emit=lines.append,
        use_colour=False,
        probes=probes,
        config=config,
        config_path="/cfg/config.toml",
        config_path_exists=False,
    )
    assert code == 1
    text = "\n".join(lines)
    assert "[FAIL]" in text
    # Each red prerequisite names its exact fix.
    assert "cloud.google.com/sdk" in text  # gcloud install
    assert "cgc setup" in text  # webhook/space/creds remediation


def test_run_doctor_secret_free_output(make_config: Callable[..., Config]) -> None:
    lines: list[str] = []
    probes = make_probes(runner=_all_green_runner(), chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    config = make_config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    run_doctor(
        env={},
        emit=lines.append,
        use_colour=False,
        probes=probes,
        config=config,
        config_path="/cfg/config.toml",
        config_path_exists=True,
    )
    text = "\n".join(lines)
    assert "SECRETTOKEN" not in text
    assert "SECRETKEY" not in text
