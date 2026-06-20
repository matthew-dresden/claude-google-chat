"""Tests for the foolproof onboarding wizard (:mod:`claude_google_chat.setup`).

Every gcloud / auth / network / clock boundary is injected via fakes, so the
whole wizard is exercised offline and deterministically. The config writes are
redirected to a per-test ``tmp_path`` so no real OS config dir is touched, and
the OAuth login indirection is patched so no browser flow runs.

Covered: idempotency (re-run skips done steps), resume after a failed step,
ADC-accepted vs ADC-rejected branches, the silent-scope-drop guard + re-auth
trigger, ``--dry-run`` changing nothing, the readiness poll (not sleep) for the
API-enable step, and the end-to-end round-trip gate failing setup when send/read
fails.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from claude_google_chat import setup as setup_mod
from claude_google_chat.config import Config
from claude_google_chat.probes import CHAT_SCOPE, CommandResult
from claude_google_chat.setup import (
    SetupError,
    SetupOptions,
    poll_until_ready,
    run_setup,
    validate_client_secrets,
)
from tests.fakes import FakeChatProbe, FakeClock, FakeCommandRunner, ScriptedIO, make_probes

WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=SECRETKEY&token=SECRETTOKEN"
SPACE_ID = "spaces/AAAA"


@pytest.fixture
def config_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every ``default_config_path`` reference setup touches to tmp.

    ``setup`` writes via ``config.merge_and_write_config`` (module-local
    ``default_config_path``) and re-loads via ``Config.load``; patch the
    ``config`` module reference so all writes/reads land in ``tmp_path``.
    """
    path = tmp_path / "config.toml"

    def _fake_path() -> Path:
        return path

    monkeypatch.setattr("claude_google_chat.config.default_config_path", _fake_path)
    monkeypatch.setattr("claude_google_chat.setup.default_config_path", _fake_path)
    return path


@pytest.fixture(autouse=True)
def _no_real_oauth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real OAuth installed-app flow with a no-op (no browser).

    A test marked ``@pytest.mark.real_oauth_indirection`` opts out so it can
    exercise the thin ``_run_oauth_login`` delegation directly.
    """
    if request.node.get_closest_marker("real_oauth_indirection"):
        return
    monkeypatch.setattr(setup_mod, "_run_oauth_login", lambda config: None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_google_chat.config import ENV_OVERRIDES

    for env_var in ENV_OVERRIDES.values():
        monkeypatch.delenv(env_var, raising=False)


def _full_runner(*, services_when_enabled: object = None) -> FakeCommandRunner:
    """A runner where gcloud is present, logged in, project set, and ADC succeeds."""
    enabled = (
        services_when_enabled
        if services_when_enabled is not None
        else CommandResult(0, stdout="chat.googleapis.com")
    )
    return FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": enabled,
            "gcloud auth application-default login": CommandResult(0),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )


def _empty_config() -> Config:
    return Config(webhook_url=None, space_id=None, oauth_client_file=None)


# --------------------------------------------------------------------------- #
# Happy path (ADC-accepted) end to end.
# --------------------------------------------------------------------------- #


def test_setup_happy_path_adc_writes_webhook_and_passes_roundtrip(
    config_redirect: Path,
) -> None:
    runner = _full_runner()
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    probes = make_probes(runner=runner, chat=chat)
    io = ScriptedIO(answers=[WEBHOOK_URL])

    code = run_setup(
        io=io,
        options=SetupOptions(),
        env={},
        probes=probes,
        config=_empty_config(),
    )
    assert code == 0
    assert "setup complete" in io.text
    # The webhook was persisted but never echoed.
    data = tomllib.loads(config_redirect.read_text(encoding="utf-8"))
    assert data["webhook_url"] == WEBHOOK_URL
    assert "SECRETTOKEN" not in io.text
    # The round-trip gate actually ran.
    assert chat.roundtrip_markers


def test_setup_uses_adc_and_does_not_prompt_for_client_file(config_redirect: Path) -> None:
    # Fresh machine: no creds at the preflight, ADC login then accepted.
    class _AdcFresh(FakeChatProbe):
        def __init__(self) -> None:
            super().__init__(roundtrip=True)
            self._seq = [[], [CHAT_SCOPE], [CHAT_SCOPE], [CHAT_SCOPE]]

        def token_scopes(self, config: Config) -> list[str]:
            self.scope_calls += 1
            return self._seq.pop(0) if self._seq else [CHAT_SCOPE]

    runner = _full_runner()
    probes = make_probes(runner=runner, chat=_AdcFresh())
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert "Application Default Credentials" in io.text
    # No OAuth-client deep links were printed (ADC accepted).
    assert "Desktop OAuth client" not in io.text
    # ADC login actually ran.
    assert any("application-default" in " ".join(c) for c in runner.calls)


# --------------------------------------------------------------------------- #
# ADC rejected -> OAuth-client fallback.
# --------------------------------------------------------------------------- #


def test_setup_falls_back_to_oauth_client_when_adc_rejected(
    config_redirect: Path, tmp_path: Path
) -> None:
    # ADC login "succeeds" but the Chat API does not accept the token initially;
    # after the OAuth-client login the probe reports the scope.
    client_file = tmp_path / "client_secret.json"
    client_file.write_text(
        '{"installed": {"client_id": "x", "client_secret": "y", '
        '"auth_uri": "https://a", "token_uri": "https://t"}}',
        encoding="utf-8",
    )

    # Scopes: preflight -> empty; ADC probe -> empty (rejected); after OAuth
    # login the scope-verify -> Chat scope.
    class _SeqChat(FakeChatProbe):
        def __init__(self) -> None:
            super().__init__(roundtrip=True)
            self._results = [[], [], [CHAT_SCOPE], [CHAT_SCOPE]]

        def token_scopes(self, config: Config) -> list[str]:
            self.scope_calls += 1
            return self._results.pop(0) if self._results else [CHAT_SCOPE]

    chat = _SeqChat()
    probes = make_probes(runner=_full_runner(), chat=chat)
    io = ScriptedIO(answers=[str(client_file), WEBHOOK_URL])

    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert "falling back to an OAuth client" in io.text
    assert "Desktop OAuth client" in io.text


def test_setup_oauth_fallback_rejects_non_desktop_client(
    config_redirect: Path, tmp_path: Path
) -> None:
    web_client = tmp_path / "web.json"
    web_client.write_text('{"web": {"client_id": "x"}}', encoding="utf-8")

    class _AdcRejected(FakeChatProbe):
        def token_scopes(self, config: Config) -> list[str]:
            self.scope_calls += 1
            return []

    probes = make_probes(runner=_full_runner(), chat=_AdcRejected())
    io = ScriptedIO(answers=[str(web_client)])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "Desktop OAuth client" in io.text


# --------------------------------------------------------------------------- #
# Scope-drop guard.
# --------------------------------------------------------------------------- #


def test_setup_detects_scope_drop_and_fails_with_reauth(config_redirect: Path) -> None:
    # ADC login succeeds and the probe returns a token, but it lacks the Chat
    # scope -> the scope guard fails fast naming the missing scope + re-auth.
    chat = FakeChatProbe(scopes=["openid"], roundtrip=True)
    probes = make_probes(runner=_full_runner(), chat=chat)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "chat.messages" in io.text
    assert "cgc setup --reauth" in io.text


# --------------------------------------------------------------------------- #
# Idempotency / resume.
# --------------------------------------------------------------------------- #


def test_setup_is_idempotent_skips_done_steps(config_redirect: Path) -> None:
    """A fully-configured environment re-runs as all-skips and still verifies."""
    runner = _full_runner()
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    probes = make_probes(runner=runner, chat=chat)
    config = Config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID, oauth_client_file=None)
    io = ScriptedIO()  # no answers needed: nothing should prompt

    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=config)
    assert code == 0
    assert "already selected" in io.text
    assert "already enabled" in io.text
    assert "already carry the required Chat scopes" in io.text
    assert "already configured" in io.text
    # ADC login must NOT have been re-run (existing creds already sufficient).
    assert not any("application-default" in " ".join(c) for c in runner.calls)


def test_setup_resumes_after_failed_webhook_step(config_redirect: Path) -> None:
    """First run fails at webhook (bad URL); second run completes from there."""
    probes = make_probes(runner=_full_runner(), chat=FakeChatProbe(scopes=[CHAT_SCOPE]))

    # Run 1: a malformed webhook URL fails the webhook step.
    io1 = ScriptedIO(answers=["https://example.com/not-a-webhook"])
    code1 = run_setup(io=io1, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code1 == 1
    assert "invalid webhook URL" in io1.text
    assert not config_redirect.exists()  # nothing persisted

    # Run 2: a good URL; auth is already satisfied (creds present), so it resumes
    # straight to webhook + verify.
    io2 = ScriptedIO(answers=[WEBHOOK_URL])
    code2 = run_setup(io=io2, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code2 == 0
    assert "setup complete" in io2.text


# --------------------------------------------------------------------------- #
# Round-trip gate.
# --------------------------------------------------------------------------- #


def test_setup_fails_when_roundtrip_does_not_read_back(config_redirect: Path) -> None:
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=False)  # sent, not read back
    probes = make_probes(runner=_full_runner(), chat=chat)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "did not read back" in io.text
    assert "cgc setup --verify" in io.text


def test_setup_fails_when_roundtrip_raises_maps_error(config_redirect: Path) -> None:
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    err = HttpError(Response({"status": 404}), b"{}", uri="https://x?token=SECRETTOKEN")
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=err)
    probes = make_probes(runner=_full_runner(), chat=chat)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "404" in io.text
    assert "SECRETTOKEN" not in io.text


# --------------------------------------------------------------------------- #
# Flags: --verify / --reauth / --dry-run.
# --------------------------------------------------------------------------- #


def test_verify_flag_runs_only_roundtrip(config_redirect: Path) -> None:
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    probes = make_probes(runner=_full_runner(), chat=chat)
    io = ScriptedIO()
    config = Config(webhook_url=WEBHOOK_URL, space_id=SPACE_ID)
    code = run_setup(io=io, options=SetupOptions(verify=True), env={}, probes=probes, config=config)
    assert code == 0
    assert "verification complete" in io.text
    assert chat.roundtrip_markers


def test_reauth_flag_runs_only_auth(config_redirect: Path) -> None:
    # No existing scopes -> ADC login runs; scope guard passes.
    class _Seq(FakeChatProbe):
        def __init__(self) -> None:
            super().__init__()
            self._seq = [[], [CHAT_SCOPE], [CHAT_SCOPE]]

        def token_scopes(self, config: Config) -> list[str]:
            self.scope_calls += 1
            return self._seq.pop(0) if self._seq else [CHAT_SCOPE]

    runner = _full_runner()
    probes = make_probes(runner=runner, chat=_Seq())
    io = ScriptedIO()
    code = run_setup(
        io=io, options=SetupOptions(reauth=True), env={}, probes=probes, config=_empty_config()
    )
    assert code == 0
    assert "re-authentication complete" in io.text
    # Webhook step did not run (no prompt consumed, none scripted).
    assert "webhook" not in io.text.lower() or "stored webhook" not in io.text


def test_dry_run_changes_nothing(config_redirect: Path) -> None:
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            # project not selected, API not enabled -> would normally act.
            "gcloud config get-value project": CommandResult(0, stdout="(unset)"),
            "gcloud services list": CommandResult(0, stdout=""),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[]))
    io = ScriptedIO()  # dry-run must not prompt
    code = run_setup(
        io=io, options=SetupOptions(dry_run=True), env={}, probes=probes, config=_empty_config()
    )
    assert code == 0
    assert "no changes made" in io.text
    # No mutating gcloud command ran.
    assert not any("create" in " ".join(c) for c in runner.calls)
    assert not any("services enable" in " ".join(c) for c in runner.calls)
    assert not any("application-default" in " ".join(c) for c in runner.calls)
    # Nothing written.
    assert not config_redirect.exists()


# --------------------------------------------------------------------------- #
# gcloud missing -> manual fallback path.
# --------------------------------------------------------------------------- #


def test_setup_without_gcloud_prints_manual_links_and_continues_auth(
    config_redirect: Path,
) -> None:
    runner = FakeCommandRunner(which_map={"gcloud": None})
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    probes = make_probes(runner=runner, chat=chat)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert "gcloud CLI not found" in io.text
    assert "console.cloud.google.com/projectcreate" in io.text


# --------------------------------------------------------------------------- #
# Project create / enable-API readiness poll.
# --------------------------------------------------------------------------- #


def test_project_create_path(config_redirect: Path) -> None:
    calls: list[list[str]] = []

    def _record(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return CommandResult(0)

    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": _project_then_set(),
            "gcloud services list": CommandResult(0, stdout="chat.googleapis.com"),
            "gcloud projects create": _record,
            "gcloud config set project": CommandResult(0),
            "gcloud auth application-default login": CommandResult(0),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    probes = make_probes(runner=runner, chat=chat)
    io = ScriptedIO(answers=["my-new-proj", WEBHOOK_URL], confirms=[True])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert any(c[:3] == ["gcloud", "projects", "create"] for c in calls)


def _project_then_set() -> object:
    """get-value project returns (unset) first, then a real id after creation."""
    state = {"n": 0}

    def _fn(argv: list[str]) -> CommandResult:
        state["n"] += 1
        return CommandResult(0, stdout="(unset)" if state["n"] == 1 else "my-new-proj")

    return _fn


def test_enable_api_polls_until_ready(config_redirect: Path) -> None:
    """The API-enable step polls (readiness) and succeeds once ENABLED appears."""
    state = {"n": 0}

    def _services(argv: list[str]) -> CommandResult:
        state["n"] += 1
        # Not enabled on the first two reads, enabled on the third.
        return CommandResult(0, stdout="chat.googleapis.com" if state["n"] >= 3 else "")

    clock = FakeClock(step=1.0)
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": _services,
            "gcloud services enable": CommandResult(0),
            "gcloud auth application-default login": CommandResult(0),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    chat = FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True)
    env = {"CGC_SETUP_ENABLE_TIMEOUT": "60", "CGC_SETUP_ENABLE_POLL_INTERVAL": "1"}
    probes = make_probes(runner=runner, chat=chat, env=env, clock=clock.now, sleeper=clock.sleep)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(
        io=io,
        options=SetupOptions(),
        env=env,
        probes=probes,
        config=_empty_config(),
    )
    assert code == 0
    assert clock.sleeps  # the readiness poll actually paced itself


def test_enable_api_poll_times_out_fails_fast(config_redirect: Path) -> None:
    clock = FakeClock(step=10.0)
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": CommandResult(0, stdout=""),  # never enabled
            "gcloud services enable": CommandResult(0),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    env = {"CGC_SETUP_ENABLE_TIMEOUT": "5", "CGC_SETUP_ENABLE_POLL_INTERVAL": "1"}
    probes = make_probes(
        runner=runner,
        chat=FakeChatProbe(scopes=[CHAT_SCOPE]),
        env=env,
        clock=clock.now,
        sleeper=clock.sleep,
    )
    io = ScriptedIO()
    code = run_setup(
        io=io,
        options=SetupOptions(),
        env=env,
        probes=probes,
        config=_empty_config(),
    )
    assert code == 1
    assert "did not report ENABLED" in io.text


def test_invalid_timeout_env_fails_fast(config_redirect: Path) -> None:
    runner = FakeCommandRunner(
        responses={
            "gcloud auth list": CommandResult(0, stdout="me@example.com"),
            "gcloud config get-value project": CommandResult(0, stdout="proj"),
            "gcloud services list": CommandResult(0, stdout=""),
            "gcloud services enable": CommandResult(0),
        },
        which_map={"gcloud": "/usr/bin/gcloud"},
    )
    env = {"CGC_SETUP_ENABLE_TIMEOUT": "not-a-number"}
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]), env=env)
    io = ScriptedIO()
    code = run_setup(
        io=io,
        options=SetupOptions(),
        env=env,
        probes=probes,
        config=_empty_config(),
    )
    assert code == 1
    assert "invalid CGC_SETUP_ENABLE_TIMEOUT" in io.text


# --------------------------------------------------------------------------- #
# Direct unit tests for the smaller pieces.
# --------------------------------------------------------------------------- #


def test_poll_until_ready_returns_when_ready_first_try() -> None:
    clock = FakeClock()
    poll_until_ready(
        is_ready=lambda: True,
        clock=clock.now,
        sleeper=clock.sleep,
        timeout=10,
        interval=1,
        timeout_message="boom",
    )
    assert clock.sleeps == []  # ready immediately -> no sleep


def test_poll_until_ready_times_out() -> None:
    clock = FakeClock(step=5.0)
    with pytest.raises(SetupError) as exc:
        poll_until_ready(
            is_ready=lambda: False,
            clock=clock.now,
            sleeper=clock.sleep,
            timeout=4,
            interval=1,
            timeout_message="timed out waiting",
        )
    assert "timed out waiting" in str(exc.value)


def test_validate_client_secrets_accepts_desktop(tmp_path: Path) -> None:
    good = tmp_path / "c.json"
    good.write_text(
        '{"installed": {"client_id": "a", "client_secret": "b", '
        '"auth_uri": "https://a", "token_uri": "https://t"}}',
        encoding="utf-8",
    )
    validate_client_secrets(str(good))  # no raise


def test_validate_client_secrets_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SetupError):
        validate_client_secrets(str(tmp_path / "nope.json"))


def test_validate_client_secrets_rejects_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "c.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(SetupError):
        validate_client_secrets(str(bad))


def test_validate_client_secrets_rejects_missing_fields(tmp_path: Path) -> None:
    partial = tmp_path / "c.json"
    partial.write_text('{"installed": {"client_id": "a"}}', encoding="utf-8")
    with pytest.raises(SetupError) as exc:
        validate_client_secrets(str(partial))
    assert "missing required field" in str(exc.value)


def test_validate_client_secrets_rejects_malformed_installed(tmp_path: Path) -> None:
    weird = tmp_path / "c.json"
    weird.write_text('{"installed": "not-a-dict"}', encoding="utf-8")
    with pytest.raises(SetupError):
        validate_client_secrets(str(weird))


# --------------------------------------------------------------------------- #
# Project-step edge cases (create-fail / select / select-fail / missing ids).
# --------------------------------------------------------------------------- #


def _base_runner(project_stdout: str = "(unset)") -> dict[str, object]:
    return {
        "gcloud auth list": CommandResult(0, stdout="me@example.com"),
        "gcloud config get-value project": CommandResult(0, stdout=project_stdout),
        "gcloud services list": CommandResult(0, stdout="chat.googleapis.com"),
        "gcloud auth application-default login": CommandResult(0),
    }


def test_project_create_failure_fails_fast(config_redirect: Path) -> None:
    responses = _base_runner()
    responses["gcloud projects create"] = CommandResult(1, stderr="exists")
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO(answers=["taken-id"], confirms=[True])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "failed to create project" in io.text


def test_project_create_missing_id_fails_fast(config_redirect: Path) -> None:
    runner = FakeCommandRunner(responses=_base_runner(), which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO(answers=[""], confirms=[True])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "project id is required" in io.text


def test_project_select_existing(config_redirect: Path) -> None:
    state = {"n": 0}

    def _proj(argv: list[str]) -> CommandResult:
        state["n"] += 1
        return CommandResult(0, stdout="(unset)" if state["n"] == 1 else "chosen-proj")

    responses = _base_runner()
    responses["gcloud config get-value project"] = _proj
    responses["gcloud config set project"] = CommandResult(0)
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True))
    io = ScriptedIO(answers=["chosen-proj", WEBHOOK_URL], confirms=[False])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert any(c[:4] == ["gcloud", "config", "set", "project"] for c in runner.calls)


def test_project_select_missing_id_fails_fast(config_redirect: Path) -> None:
    runner = FakeCommandRunner(responses=_base_runner(), which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO(answers=[""], confirms=[False])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "project id is required" in io.text


def test_project_select_failure_fails_fast(config_redirect: Path) -> None:
    responses = _base_runner()
    responses["gcloud config set project"] = CommandResult(1, stderr="nope")
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO(answers=["bad-proj"], confirms=[False])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "failed to select project" in io.text


def test_project_postflight_failure_fails_fast(config_redirect: Path) -> None:
    """Selection 'succeeds' but the project is still unset on postflight."""
    responses = _base_runner()  # get-value always returns (unset)
    responses["gcloud config set project"] = CommandResult(0)
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO(answers=["ghost-proj"], confirms=[False])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "did not take effect" in io.text


def test_enable_api_command_failure_fails_fast(config_redirect: Path) -> None:
    responses = _base_runner("proj")
    responses["gcloud services list"] = CommandResult(0, stdout="")  # not enabled
    responses["gcloud services enable"] = CommandResult(1, stderr="billing")
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE]))
    io = ScriptedIO()
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "failed to enable" in io.text


def test_adc_login_command_failure_falls_back_to_oauth(
    config_redirect: Path, tmp_path: Path
) -> None:
    client = tmp_path / "client.json"
    client.write_text(
        '{"installed": {"client_id": "a", "client_secret": "b", '
        '"auth_uri": "https://a", "token_uri": "https://t"}}',
        encoding="utf-8",
    )

    class _Seq(FakeChatProbe):
        def __init__(self) -> None:
            super().__init__(roundtrip=True)
            self._seq = [[], [CHAT_SCOPE], [CHAT_SCOPE]]

        def token_scopes(self, config: Config) -> list[str]:
            self.scope_calls += 1
            return self._seq.pop(0) if self._seq else [CHAT_SCOPE]

    responses = _base_runner("proj")
    responses["gcloud auth application-default login"] = CommandResult(1, stderr="cancelled")
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(runner=runner, chat=_Seq())
    io = ScriptedIO(answers=[str(client), WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0
    assert "ADC login did not complete" in io.text


def test_oauth_fallback_missing_client_path_fails_fast(config_redirect: Path) -> None:
    class _Rejected(FakeChatProbe):
        def token_scopes(self, config: Config) -> list[str]:
            return []

    runner = FakeCommandRunner(
        responses=_base_runner("proj"), which_map={"gcloud": "/usr/bin/gcloud"}
    )
    probes = make_probes(runner=runner, chat=_Rejected())
    io = ScriptedIO(answers=[""])  # empty client path
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 1
    assert "client-secrets JSON path is required" in io.text


def test_webhook_step_revalidates_existing_malformed(config_redirect: Path) -> None:
    """A pre-existing malformed webhook is re-prompted (not trusted)."""
    runner = _full_runner()
    probes = make_probes(runner=runner, chat=FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True))
    config = Config(webhook_url="https://example.com/bad", space_id=SPACE_ID)
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=config)
    assert code == 0
    assert "stored webhook_url" in io.text


def test_enable_api_uses_default_timeouts_when_env_unset(config_redirect: Path) -> None:
    """No CGC_SETUP_ENABLE_* env vars -> the readiness poll uses defaults."""
    state = {"n": 0}

    def _services(argv: list[str]) -> CommandResult:
        state["n"] += 1
        return CommandResult(0, stdout="chat.googleapis.com" if state["n"] >= 2 else "")

    clock = FakeClock(step=1.0)
    responses = _base_runner("proj")
    responses["gcloud services list"] = _services
    responses["gcloud services enable"] = CommandResult(0)
    runner = FakeCommandRunner(responses=responses, which_map={"gcloud": "/usr/bin/gcloud"})
    probes = make_probes(
        runner=runner,
        chat=FakeChatProbe(scopes=[CHAT_SCOPE], roundtrip=True),
        clock=clock.now,
        sleeper=clock.sleep,
    )
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    assert code == 0


def test_token_scopes_or_empty_swallows_probe_error(config_redirect: Path) -> None:
    """A raising token_scopes during the auth preflight degrades to empty (no crash)."""

    class _Raises(FakeChatProbe):
        def __init__(self) -> None:
            super().__init__(roundtrip=True)
            self._calls = 0

        def token_scopes(self, config: Config) -> list[str]:
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("token read blew up")
            return [CHAT_SCOPE]

    runner = _full_runner()
    probes = make_probes(runner=runner, chat=_Raises())
    io = ScriptedIO(answers=[WEBHOOK_URL])
    code = run_setup(io=io, options=SetupOptions(), env={}, probes=probes, config=_empty_config())
    # Preflight raised -> treated as no creds -> ADC runs and is then accepted.
    assert code == 0
    assert any("application-default" in " ".join(c) for c in runner.calls)


def test_typer_setup_io_delegates_to_callables() -> None:
    from claude_google_chat.setup import TyperSetupIO

    prompts: list[str] = []
    emits: list[str] = []
    confirms: list[str] = []
    io = TyperSetupIO(
        _prompt=lambda m: prompts.append(m) or "answer",
        _emit=emits.append,
        _confirm=lambda m: confirms.append(m) or True,
    )
    assert io.prompt("q?") == "answer"
    io.emit("a line")
    assert io.confirm("yes?") is True
    assert prompts == ["q?"]
    assert emits == ["a line"]
    assert confirms == ["yes?"]


@pytest.mark.real_oauth_indirection
def test_run_oauth_login_delegates_to_auth_login(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    monkeypatch.setattr("claude_google_chat.auth.login", lambda config: called.append(config))
    setup_mod._run_oauth_login(Config())
    assert len(called) == 1


def test_dry_run_reauth_reports_no_changes(config_redirect: Path) -> None:
    probes = make_probes(runner=_full_runner(), chat=FakeChatProbe(scopes=[]))
    io = ScriptedIO()
    code = run_setup(
        io=io,
        options=SetupOptions(reauth=True, dry_run=True),
        env={},
        probes=probes,
        config=_empty_config(),
    )
    assert code == 0
    assert "no changes made" in io.text
