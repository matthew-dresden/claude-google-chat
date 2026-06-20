"""Targeted tests closing the remaining coverage gaps across the package.

Each test here exercises a specific previously-uncovered line or branch:

- ``chat._build_service`` (lazy ``build`` wiring).
- ``rawmessage.is_human_message`` non-dict sender.
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

from claude_google_chat import chat as chat_module
from claude_google_chat import config as config_module
from claude_google_chat.config import Config, _toml_value, default_config_path
from claude_google_chat.rawmessage import is_human_message, thread_name


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "space_id": "spaces/AAAA",
    }
    base.update(overrides)
    return Config(**base)


# --------------------------------------------------------------------------- #
# chat._build_service — lazy discovery build wiring.
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


# --------------------------------------------------------------------------- #
# raw-message accessors — non-dict sender branch.
# --------------------------------------------------------------------------- #


def test_is_human_message_returns_false_for_non_dict_sender() -> None:
    """A non-dict ``sender`` is not classified as a human message."""
    assert is_human_message({"sender": None}) is False


def test_thread_name_extracts_resource_name() -> None:
    """A present ``thread.name`` is returned verbatim."""
    raw = {"thread": {"name": "spaces/AAAA/threads/T9"}}
    assert thread_name(raw) == "spaces/AAAA/threads/T9"


def test_thread_name_returns_none_for_non_dict_thread() -> None:
    """A non-dict ``thread`` yields ``None`` instead of raising."""
    assert thread_name({"thread": None}) is None


def test_thread_name_returns_none_when_thread_absent() -> None:
    """A message with no ``thread`` key yields ``None``."""
    assert thread_name({"text": "hi"}) is None


def test_thread_name_returns_none_for_empty_name() -> None:
    """An empty thread name string is treated as absent (``None``)."""
    assert thread_name({"thread": {"name": ""}}) is None


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
