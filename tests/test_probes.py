"""Tests for the injectable probes + pure doctor checks (:mod:`claude_google_chat.probes`).

Each pure check is driven with fake probes so both its GREEN and RED branches are
asserted — including that a RED line carries the exact fix command. The webhook
validator is checked for shape acceptance/rejection without ever echoing the
token. No subprocess or network runs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage
from claude_google_chat.probes import (
    CHAT_SCOPE,
    ChatApiProbe,
    CommandResult,
    SubprocessRunner,
    check_chat_api_enabled,
    check_config_file_present,
    check_credentials_present,
    check_gcloud_installed,
    check_gcloud_logged_in,
    check_project_selected,
    check_space_configured,
    check_token_scopes,
    check_webhook_configured,
    production_probes,
    run_all_checks,
    validate_webhook_url,
)
from tests.fakes import FakeChatProbe, FakeCommandRunner, make_probes

WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=SECRETKEY&token=SECRETTOKEN"
SPACE_ID = "spaces/AAAA"


# --------------------------------------------------------------------------- #
# gcloud presence / login / project / API.
# --------------------------------------------------------------------------- #


def test_gcloud_installed_green_when_resolvable() -> None:
    probes = make_probes(runner=FakeCommandRunner(which_map={"gcloud": "/usr/bin/gcloud"}))
    result = check_gcloud_installed(probes)
    assert result.ok is True


def test_gcloud_installed_red_with_install_link() -> None:
    probes = make_probes(runner=FakeCommandRunner(which_map={"gcloud": None}))
    result = check_gcloud_installed(probes)
    assert result.ok is False
    assert "cloud.google.com/sdk" in result.fix


def test_gcloud_logged_in_green_when_active_account() -> None:
    runner = FakeCommandRunner(
        responses={"gcloud auth list": CommandResult(returncode=0, stdout="me@example.com\n")}
    )
    assert check_gcloud_logged_in(make_probes(runner=runner)).ok is True


def test_gcloud_logged_in_red_when_no_account() -> None:
    runner = FakeCommandRunner(
        responses={"gcloud auth list": CommandResult(returncode=0, stdout="")}
    )
    result = check_gcloud_logged_in(make_probes(runner=runner))
    assert result.ok is False
    assert "gcloud auth login" in result.fix


def test_project_selected_green() -> None:
    runner = FakeCommandRunner(
        responses={"gcloud config get-value project": CommandResult(0, stdout="my-proj\n")}
    )
    result = check_project_selected(make_probes(runner=runner))
    assert result.ok is True
    assert "my-proj" in result.name


def test_project_selected_red_when_unset() -> None:
    runner = FakeCommandRunner(
        responses={"gcloud config get-value project": CommandResult(0, stdout="(unset)\n")}
    )
    result = check_project_selected(make_probes(runner=runner))
    assert result.ok is False
    assert "cgc setup" in result.fix


def test_chat_api_enabled_green() -> None:
    runner = FakeCommandRunner(
        responses={"gcloud services list": CommandResult(0, stdout="chat.googleapis.com\n")}
    )
    assert check_chat_api_enabled(make_probes(runner=runner)).ok is True


def test_chat_api_enabled_red_when_absent() -> None:
    runner = FakeCommandRunner(responses={"gcloud services list": CommandResult(0, stdout="")})
    result = check_chat_api_enabled(make_probes(runner=runner))
    assert result.ok is False
    assert "chat.googleapis.com" in result.fix


# --------------------------------------------------------------------------- #
# credentials + scopes.
# --------------------------------------------------------------------------- #


def test_credentials_present_green(make_config: Callable[..., Config]) -> None:
    probes = make_probes(chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    assert check_credentials_present(probes, make_config()).ok is True


def test_credentials_present_red_with_fix(make_config: Callable[..., Config]) -> None:
    probes = make_probes(chat=FakeChatProbe(scopes=FileNotFoundError("no token")))
    result = check_credentials_present(probes, make_config())
    assert result.ok is False
    assert "cgc setup" in result.fix


def test_token_scopes_green(make_config: Callable[..., Config]) -> None:
    probes = make_probes(chat=FakeChatProbe(scopes=[CHAT_SCOPE, "openid"]))
    assert check_token_scopes(probes, make_config()).ok is True


def test_token_scopes_red_names_missing_scope(make_config: Callable[..., Config]) -> None:
    probes = make_probes(chat=FakeChatProbe(scopes=["openid"]))
    result = check_token_scopes(probes, make_config())
    assert result.ok is False
    assert "chat.messages" in result.fix


def test_token_scopes_red_when_unreadable(make_config: Callable[..., Config]) -> None:
    probes = make_probes(chat=FakeChatProbe(scopes=ValueError("bad token")))
    result = check_token_scopes(probes, make_config())
    assert result.ok is False
    assert "cgc setup" in result.fix


# --------------------------------------------------------------------------- #
# webhook / space / config-file.
# --------------------------------------------------------------------------- #


def test_webhook_configured_green(make_config: Callable[..., Config]) -> None:
    assert check_webhook_configured(make_config(webhook_url=WEBHOOK_URL)).ok is True


def test_webhook_configured_red_when_absent(make_config: Callable[..., Config]) -> None:
    result = check_webhook_configured(make_config(webhook_url=None))
    assert result.ok is False
    assert "cgc setup" in result.fix


def test_webhook_configured_red_when_malformed(make_config: Callable[..., Config]) -> None:
    result = check_webhook_configured(make_config(webhook_url="https://example.com/nope"))
    assert result.ok is False
    # The token is never part of the message (none present here, but the rule holds).
    assert "SECRETTOKEN" not in result.fix


def test_space_configured_green(make_config: Callable[..., Config]) -> None:
    assert check_space_configured(make_config(space_id=SPACE_ID)).ok is True


def test_space_configured_red_when_absent(make_config: Callable[..., Config]) -> None:
    result = check_space_configured(make_config(space_id=None))
    assert result.ok is False
    assert "cgc setup" in result.fix


def test_space_configured_red_when_malformed(make_config: Callable[..., Config]) -> None:
    result = check_space_configured(make_config(space_id="not-a-space"))
    assert result.ok is False


def test_config_file_present_green() -> None:
    result = check_config_file_present(True, "/cfg/config.toml")
    assert result.ok is True
    assert "/cfg/config.toml" in result.name


def test_config_file_present_red_is_optional() -> None:
    result = check_config_file_present(False, "/cfg/config.toml")
    assert result.ok is False
    assert result.required is False
    assert "cgc config init" in result.fix


# --------------------------------------------------------------------------- #
# webhook URL validator.
# --------------------------------------------------------------------------- #


def test_validate_webhook_url_accepts_well_formed() -> None:
    assert validate_webhook_url(WEBHOOK_URL) == WEBHOOK_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://chat.googleapis.com/v1/spaces/AAAA/messages?key=k&token=t",  # not https
        "https://example.com/v1/spaces/AAAA/messages?key=k&token=t",  # wrong host
        "https://chat.googleapis.com/v1/spaces/AAAA?key=k&token=t",  # no /messages
        "https://chat.googleapis.com/v1/spaces/AAAA/messages?token=t",  # no key
        "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=k",  # no token
    ],
)
def test_validate_webhook_url_rejects_malformed(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


# --------------------------------------------------------------------------- #
# aggregate report.
# --------------------------------------------------------------------------- #


def test_run_all_checks_all_green(make_config: Callable[..., Config]) -> None:
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": CommandResult(0, stdout="chat.googleapis.com"),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    config = make_config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    report = run_all_checks(probes, config, config_path_exists=True, config_path="/cfg/config.toml")
    assert report.ok is True
    assert all(c.ok for c in report.checks)


def test_run_all_checks_red_required_fails_report(make_config: Callable[..., Config]) -> None:
    runner = FakeCommandRunner(which_map={"gcloud": None})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=FileNotFoundError("x")))
    config = make_config(webhook_url=None, space_id=None)
    report = run_all_checks(
        probes, config, config_path_exists=False, config_path="/cfg/config.toml"
    )
    assert report.ok is False


def test_run_all_checks_only_optional_red_still_passes(make_config: Callable[..., Config]) -> None:
    """A missing config file (optional) alone does not fail the report."""
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": CommandResult(0, stdout="chat.googleapis.com"),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    config = make_config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    report = run_all_checks(
        probes, config, config_path_exists=False, config_path="/cfg/config.toml"
    )
    assert report.ok is True


def test_production_probes_wires_real_collaborators() -> None:
    probes = production_probes({"PATH": "/usr/bin"})
    # which on a non-existent program returns None (no exception) and clock works.
    assert probes.runner.which("definitely-not-a-real-binary-xyz") is None
    assert isinstance(probes.clock(), float)


# --------------------------------------------------------------------------- #
# Production collaborators (hermetic: no gcloud, no network).
# --------------------------------------------------------------------------- #


def test_subprocess_runner_captures_exit_and_streams() -> None:
    runner = SubprocessRunner()
    ok = runner.run(["printf", "hello"])
    assert ok.ok is True
    assert ok.stdout == "hello"
    bad = runner.run(["false"])
    assert bad.ok is False


def test_subprocess_runner_missing_executable_is_nonzero_not_raise() -> None:
    runner = SubprocessRunner()
    result = runner.run(["definitely-not-a-real-binary-xyz"])
    assert result.ok is False
    assert result.returncode == 127


def test_subprocess_runner_which_resolves_real_binary() -> None:
    runner = SubprocessRunner()
    assert runner.which("definitely-not-a-real-binary-xyz") is None


def test_chat_api_probe_token_scopes_reads_credentials(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    class _Creds:
        scopes = [CHAT_SCOPE, "openid"]

    monkeypatch.setattr("claude_google_chat.auth.load_credentials", lambda config: _Creds())
    assert ChatApiProbe().token_scopes(make_config()) == [CHAT_SCOPE, "openid"]


def test_chat_api_probe_token_scopes_empty_when_none(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    class _Creds:
        scopes = None

    monkeypatch.setattr("claude_google_chat.auth.load_credentials", lambda config: _Creds())
    assert ChatApiProbe().token_scopes(make_config()) == []


def test_chat_api_probe_roundtrip_true_when_marker_reads_back(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    sent: list[str] = []

    def _send(config: Config, msg: ChatMessage, thread_key: str | None = None) -> None:
        sent.append(msg.text)
        return None

    monkeypatch.setattr("claude_google_chat.chat.send_webhook", _send)
    monkeypatch.setattr(
        "claude_google_chat.chat.list_messages",
        lambda config: [{"text": "cgc-setup-verify-abc and more"}],
    )
    assert ChatApiProbe().send_and_read_back(make_config(), "cgc-setup-verify-abc") is True
    assert sent == ["cgc-setup-verify-abc"]


def test_chat_api_probe_roundtrip_false_when_absent(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    monkeypatch.setattr(
        "claude_google_chat.chat.send_webhook", lambda config, msg, thread_key=None: None
    )
    monkeypatch.setattr(
        "claude_google_chat.chat.list_messages", lambda config: [{"text": "something else"}]
    )
    assert ChatApiProbe().send_and_read_back(make_config(), "missing-marker") is False
