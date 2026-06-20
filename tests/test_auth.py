"""Unit tests for user-OAuth credential loading (``auth.py``).

The heavy network/browser boundaries are mocked per-test: the
``InstalledAppFlow`` installed-app flow and the OAuth ``Credentials`` user-token
loader are replaced via ``unittest.mock`` so no Google call, browser, or real
token file is required. Config is supplied by ``make_config`` and token/client
files live under ``tmp_path``. Missing-file paths must fail fast with actionable
errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from claude_google_chat import auth

# --------------------------------------------------------------------------- #
# load_credentials (user OAuth token cache).
# --------------------------------------------------------------------------- #


def _write_token(path: Path) -> Path:
    path.write_text('{"token": "fake", "refresh_token": "r"}', encoding="utf-8")
    return path


def test_load_credentials_returns_valid_cached_token(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    tmp_path: Path,
) -> None:
    token = _write_token(tmp_path / "token.json")
    config = make_config(token_file=str(token))

    creds = MagicMock(valid=True)
    from_file = MagicMock(return_value=creds)
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file", from_file
    )

    result = auth.load_credentials(config)

    assert result is creds
    from_file.assert_called_once_with(str(token), auth.CHAT_SCOPES)


def test_load_credentials_refreshes_expired_token(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    tmp_path: Path,
) -> None:
    token = _write_token(tmp_path / "token.json")
    config = make_config(token_file=str(token))

    creds = MagicMock(valid=False, expired=True, refresh_token="r")
    creds.to_json.return_value = '{"token": "refreshed"}'
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        MagicMock(return_value=creds),
    )
    request_cls = MagicMock()
    monkeypatch.setattr("google.auth.transport.requests.Request", request_cls)

    result = auth.load_credentials(config)

    assert result is creds
    creds.refresh.assert_called_once()
    # The refreshed token is re-cached with owner-only permissions.
    assert token.read_text(encoding="utf-8") == '{"token": "refreshed"}'
    assert (token.stat().st_mode & 0o777) == 0o600


def test_load_credentials_raises_when_unrefreshable(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    tmp_path: Path,
) -> None:
    token = _write_token(tmp_path / "token.json")
    config = make_config(token_file=str(token))

    # Invalid, not expired, and no refresh token -> cannot recover.
    creds = MagicMock(valid=False, expired=False, refresh_token=None)
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        MagicMock(return_value=creds),
    )

    with pytest.raises(ValueError) as exc_info:
        auth.load_credentials(config)
    assert "cgc auth login" in str(exc_info.value)


def test_load_credentials_missing_token_file(
    make_config: Any,
    tmp_path: Path,
) -> None:
    config = make_config(token_file=str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError) as exc_info:
        auth.load_credentials(config)
    assert "cgc auth login" in str(exc_info.value)


def test_load_credentials_missing_token_config(make_config: Any) -> None:
    config = make_config(token_file=None)
    with pytest.raises(ValueError) as exc_info:
        auth.load_credentials(config)
    assert "token_file" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# login (installed-app OAuth flow).
# --------------------------------------------------------------------------- #


def test_login_runs_flow_and_caches_token(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Any,
    tmp_path: Path,
) -> None:
    client = tmp_path / "client_secret.json"
    client.write_text('{"installed": {}}', encoding="utf-8")
    token = tmp_path / "nested" / "token.json"
    config = make_config(oauth_client_file=str(client), token_file=str(token))

    creds = MagicMock()
    creds.to_json.return_value = '{"token": "new"}'
    flow = MagicMock()
    flow.run_local_server.return_value = creds
    flow_factory = MagicMock(return_value=flow)
    monkeypatch.setattr(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", flow_factory
    )

    result = auth.login(config)

    assert result is creds
    flow_factory.assert_called_once_with(str(client), auth.CHAT_SCOPES)
    flow.run_local_server.assert_called_once_with(port=0)
    # Parent dir is created and the token cached with 0600 perms.
    assert token.read_text(encoding="utf-8") == '{"token": "new"}'
    assert (token.stat().st_mode & 0o777) == 0o600


def test_login_missing_client_config(make_config: Any) -> None:
    config = make_config(oauth_client_file=None)
    with pytest.raises(ValueError) as exc_info:
        auth.login(config)
    assert "oauth_client_file" in str(exc_info.value)


def test_login_missing_client_file(
    make_config: Any,
    tmp_path: Path,
) -> None:
    config = make_config(oauth_client_file=str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError) as exc_info:
        auth.login(config)
    assert "client secrets" in str(exc_info.value)
