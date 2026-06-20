"""Targeted tests closing the remaining coverage gaps across the package.

Each test here exercises a specific previously-uncovered line or branch:

- ``chat._build_service`` / ``chat.build_app_service`` (lazy ``build`` wiring).
- ``serve._message_sender_email`` / ``serve._is_app_message`` non-dict senders.
- ``serve.run`` idle-timeout → non-zero exit path.
- ``bootstrap.normalize_pubsub_topic`` final-regex rejection.
- ``bootstrap._build_events_service`` (lazy ``build`` wiring).
- ``bootstrap._ensure_space`` malformed configured space id.
- ``bootstrap._ensure_space`` / ``_create_subscription`` re-raise of unrelated
  ``HttpError`` (not a not-configured marker).
- ``config.default_config_path`` and ``config._toml_value`` boolean-true literal.
- ``cli`` ``python -m`` entrypoint dispatch.

All boundaries (Google client builders, credentials, clocks) are injected or
monkeypatched so the suite stays offline and deterministic. No assertion is
weakened and no coverage pragma is used.
"""

from __future__ import annotations

import runpy
import sys
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from claude_google_chat import bootstrap as bootstrap_module
from claude_google_chat import chat as chat_module
from claude_google_chat import config as config_module
from claude_google_chat import serve as serve_module
from claude_google_chat.bootstrap import (
    ChatAppNotConfiguredError,
    _create_subscription,
    _ensure_space,
    normalize_pubsub_topic,
)
from claude_google_chat.config import Config, _toml_value, default_config_path
from claude_google_chat.serve import (
    ServeTimeout,
    _is_app_message,
    _message_sender_email,
)


def _http_error(status: int, message: str) -> HttpError:
    """Build a googleapiclient ``HttpError`` whose ``str()`` embeds ``message``."""
    resp = Response({"status": status})
    content = ('{"error": {"message": "' + message + '"}}').encode("utf-8")
    return HttpError(resp, content, uri="https://chat.googleapis.com")


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "service_account_file": "/tmp/sa.json",
        "project_id": "test-project",
        "pubsub_topic": "projects/test-project/topics/chat-events",
        "space_id": "spaces/AAAA",
    }
    base.update(overrides)
    return Config(**base)


# --------------------------------------------------------------------------- #
# chat._build_service / chat.build_app_service — lazy discovery build wiring.
# --------------------------------------------------------------------------- #


def test_build_service_wires_user_credentials_into_discovery_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_service`` loads user OAuth creds and builds the chat v1 client."""
    sentinel_creds = object()
    sentinel_service = object()
    captured: dict[str, Any] = {}

    def fake_load_credentials(config: Config) -> object:
        captured["config"] = config
        return sentinel_creds

    def fake_build(service: str, version: str, **kwargs: Any) -> object:
        captured["build_args"] = (service, version, kwargs)
        return sentinel_service

    monkeypatch.setattr(chat_module, "load_credentials", fake_load_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)

    config = _config()
    result = chat_module._build_service(config)

    assert result is sentinel_service
    assert captured["config"] is config
    service, version, kwargs = captured["build_args"]
    assert (service, version) == ("chat", "v1")
    assert kwargs["credentials"] is sentinel_creds
    assert kwargs["cache_discovery"] is False


def test_build_app_service_wires_service_account_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_app_service`` loads app creds and builds the chat v1 client."""
    sentinel_creds = object()
    sentinel_service = object()
    captured: dict[str, Any] = {}

    def fake_load_app_credentials(config: Config) -> object:
        captured["config"] = config
        return sentinel_creds

    def fake_build(service: str, version: str, **kwargs: Any) -> object:
        captured["build_args"] = (service, version, kwargs)
        return sentinel_service

    monkeypatch.setattr(chat_module, "load_app_credentials", fake_load_app_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)

    config = _config()
    result = chat_module.build_app_service(config)

    assert result is sentinel_service
    assert captured["config"] is config
    service, version, kwargs = captured["build_args"]
    assert (service, version) == ("chat", "v1")
    assert kwargs["credentials"] is sentinel_creds
    assert kwargs["cache_discovery"] is False


# --------------------------------------------------------------------------- #
# serve helpers — non-dict / missing sender branches.
# --------------------------------------------------------------------------- #


def test_message_sender_email_returns_none_for_non_dict_sender() -> None:
    """A non-dict ``sender`` yields no extractable email."""
    assert _message_sender_email({"sender": "not-a-dict"}) is None


def test_message_sender_email_returns_none_for_missing_email() -> None:
    """A dict ``sender`` without an ``email`` key yields None."""
    assert _message_sender_email({"sender": {"type": "HUMAN"}}) is None


def test_is_app_message_returns_false_for_non_dict_sender() -> None:
    """A non-dict ``sender`` is not classified as a bot/app message."""
    assert _is_app_message({"sender": None}) is False


# --------------------------------------------------------------------------- #
# serve.run — idle-timeout maps to a non-zero exit with a stderr diagnostic.
# --------------------------------------------------------------------------- #


def test_run_returns_nonzero_on_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``serve.run`` returns 1 and writes the timeout message to stderr."""
    config = _config(listen_timeout=5, poll_interval=0)

    class _ImmediateTimeoutResponder:
        def __init__(self, cfg: Config) -> None:
            self._cfg = cfg

        def run(self, *, once: bool = False) -> list[Any]:
            raise ServeTimeout("no Google Chat owner messages handled within 5s idle timeout")

    monkeypatch.setattr(serve_module, "Responder", _ImmediateTimeoutResponder)

    exit_code = serve_module.run(config, once=False)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "idle timeout" in captured.err


def test_run_returns_zero_on_clean_once_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean ``--once`` drain returns exit code 0 (no timeout raised)."""
    config = _config(listen_timeout=5, poll_interval=0)

    class _NoopResponder:
        def __init__(self, cfg: Config) -> None:
            self._cfg = cfg

        def run(self, *, once: bool = False) -> list[Any]:
            assert once is True
            return []

    monkeypatch.setattr(serve_module, "Responder", _NoopResponder)

    assert serve_module.run(config, once=True) == 0


# --------------------------------------------------------------------------- #
# bootstrap.normalize_pubsub_topic — final regex rejection of a qualified value.
# --------------------------------------------------------------------------- #


def test_normalize_pubsub_topic_rejects_malformed_qualified_topic() -> None:
    """A ``projects/...`` string that is not a valid topic resource fails fast."""
    with pytest.raises(ValueError, match="invalid Pub/Sub topic"):
        normalize_pubsub_topic("test-project", "projects/only-one-segment")


# --------------------------------------------------------------------------- #
# bootstrap._build_events_service — lazy discovery build wiring (app creds).
# --------------------------------------------------------------------------- #


def test_build_events_service_wires_app_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_events_service`` loads app creds and builds workspaceevents v1."""
    from claude_google_chat import auth as auth_module

    sentinel_creds = object()
    sentinel_service = object()
    captured: dict[str, Any] = {}

    def fake_load_app_credentials(config: Config, *, scopes: Any) -> object:
        captured["config"] = config
        captured["scopes"] = scopes
        return sentinel_creds

    def fake_build(service: str, version: str, **kwargs: Any) -> object:
        captured["build_args"] = (service, version, kwargs)
        return sentinel_service

    monkeypatch.setattr(auth_module, "load_app_credentials", fake_load_app_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)

    config = _config()
    result = bootstrap_module._build_events_service(config)

    assert result is sentinel_service
    assert captured["config"] is config
    assert captured["scopes"] == auth_module.APP_SCOPES
    service, version, kwargs = captured["build_args"]
    assert (service, version) == ("workspaceevents", "v1")
    assert kwargs["credentials"] is sentinel_creds
    assert kwargs["cache_discovery"] is False


# --------------------------------------------------------------------------- #
# bootstrap._ensure_space — malformed configured space id fails fast.
# --------------------------------------------------------------------------- #


def test_ensure_space_rejects_malformed_configured_space_id(
    fake_chat_service: Any,
) -> None:
    """A configured but malformed ``space_id`` is rejected before any API call."""
    config = _config(space_id="not-a-space")
    with pytest.raises(ValueError, match="invalid space id"):
        _ensure_space(config, fake_chat_service)
    assert fake_chat_service.member_create_calls == []


# --------------------------------------------------------------------------- #
# bootstrap._ensure_space — unrelated HttpError on space create re-raises.
# --------------------------------------------------------------------------- #


def test_ensure_space_create_reraises_unrelated_http_error(
    fake_chat_service: Any,
) -> None:
    """A non-not-configured error from ``spaces.create`` propagates unchanged."""
    config = _config(space_id=None, space_display_name="My Space")
    fake_chat_service.space_create_error = _http_error(500, "INTERNAL transient failure")

    with pytest.raises(HttpError):
        _ensure_space(config, fake_chat_service)


def test_ensure_space_create_maps_not_configured_error(
    fake_chat_service: Any,
) -> None:
    """A PERMISSION_DENIED on create surfaces the actionable instructions."""
    config = _config(space_id=None, space_display_name="My Space")
    fake_chat_service.space_create_error = _http_error(403, "PERMISSION_DENIED for caller")

    with pytest.raises(ChatAppNotConfiguredError):
        _ensure_space(config, fake_chat_service)


# --------------------------------------------------------------------------- #
# bootstrap._create_subscription — unrelated HttpError re-raises.
# --------------------------------------------------------------------------- #


def test_create_subscription_reraises_unrelated_http_error() -> None:
    """A non-not-configured, non-409 error from subscriptions.create propagates."""

    class _ErroringEvents:
        def subscriptions(self) -> _ErroringEvents:
            return self

        def create(self, *, body: dict[str, Any]) -> _ErroringEvents:
            return self

        def execute(self) -> Any:
            raise _http_error(500, "INTERNAL transient failure")

    config = _config()
    with pytest.raises(HttpError):
        _create_subscription(config, _ErroringEvents(), "spaces/AAAA", config.pubsub_topic)


def test_create_subscription_maps_not_configured_error() -> None:
    """A NOT_FOUND error from subscriptions.create surfaces the instructions."""

    class _ErroringEvents:
        def subscriptions(self) -> _ErroringEvents:
            return self

        def create(self, *, body: dict[str, Any]) -> _ErroringEvents:
            return self

        def execute(self) -> Any:
            raise _http_error(404, "NOT_FOUND: Chat app")

    config = _config()
    with pytest.raises(ChatAppNotConfiguredError):
        _create_subscription(config, _ErroringEvents(), "spaces/AAAA", config.pubsub_topic)


# --------------------------------------------------------------------------- #
# config — default_config_path and the boolean-true TOML literal.
# --------------------------------------------------------------------------- #


def test_default_config_path_is_config_toml_under_config_dir() -> None:
    """``default_config_path`` lives at ``config.toml`` under the config dir."""
    path = default_config_path()
    assert path.name == "config.toml"
    assert path.parent == config_module.config_dir()


def test_toml_value_serialises_boolean_true() -> None:
    """``_toml_value`` emits the lowercase ``true`` literal for boolean True."""
    assert _toml_value(True) == "true"
    assert _toml_value(False) == "false"


# --------------------------------------------------------------------------- #
# cli — ``python -m`` style entrypoint dispatch invokes the Typer app.
# --------------------------------------------------------------------------- #


def test_cli_module_main_guard_invokes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executing ``cli`` as ``__main__`` dispatches to the Typer ``app``.

    Typer/Click exits via ``SystemExit`` after parsing ``argv``; we pin ``argv``
    to the no-arg invocation (which prints usage and exits non-zero) and assert
    the guard reached ``app()`` by observing that ``SystemExit``.
    """
    monkeypatch.setattr(sys, "argv", ["cgc"])
    # Drop the cached module so runpy executes its body fresh under __main__.
    monkeypatch.delitem(sys.modules, "claude_google_chat.cli", raising=False)
    with pytest.raises(SystemExit):
        runpy.run_module("claude_google_chat.cli", run_name="__main__")


def test_package_dunder_main_invokes_cli_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m claude_google_chat`` routes through ``__main__.main`` to app."""
    import claude_google_chat.__main__ as dunder_main

    called: dict[str, bool] = {"app": False}

    def fake_app() -> None:
        called["app"] = True

    monkeypatch.setattr(dunder_main, "app", fake_app)
    dunder_main.main()

    assert called["app"] is True


def test_package_dunder_main_module_guard_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running ``claude_google_chat.__main__`` as ``__main__`` calls ``main``.

    Covers the ``if __name__ == "__main__": main()`` guard. ``argv`` is pinned to
    a no-arg invocation so Typer parses, prints usage, and exits via SystemExit.
    """
    monkeypatch.setattr(sys, "argv", ["cgc"])
    monkeypatch.delitem(sys.modules, "claude_google_chat.__main__", raising=False)
    with pytest.raises(SystemExit):
        runpy.run_module("claude_google_chat.__main__", run_name="__main__")
