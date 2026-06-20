"""Google OAuth (installed-app flow) for Google Chat API access.

Outbound sends use the incoming webhook and require no OAuth; OAuth is only
needed for reading/listening/deleting via the Chat REST API. Tokens are never
logged and are cached with owner-only (0600) permissions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from claude_google_chat.config import Config

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials

# Read + send scope; send happens via webhook but the scope covers API reads.
CHAT_SCOPES: list[str] = ["https://www.googleapis.com/auth/chat.messages"]

# App (service-account) scopes used by 'cgc bootstrap' and 'cgc serve'.
# The bot app reads/posts messages, manages its space memberships, creates
# spaces, and registers Google Workspace Events subscriptions.
APP_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.memberships",
]


def _require_client_file(config: Config) -> Path:
    """Return the OAuth client secrets path, raising if it is absent."""
    if not config.oauth_client_file:
        raise ValueError(
            "missing required config value 'oauth_client_file' "
            "(set CGC_OAUTH_CLIENT_FILE or add it to config.toml)"
        )
    client_path = Path(config.oauth_client_file)
    if not client_path.exists():
        raise FileNotFoundError(f"OAuth client secrets file not found: {client_path}")
    return client_path


def _token_path(config: Config) -> Path:
    """Return the cached token path, raising if unconfigured."""
    if not config.token_file:
        raise ValueError("missing required config value 'token_file'")
    return Path(config.token_file)


def load_credentials(config: Config) -> Credentials:
    """Load cached OAuth credentials, refreshing them if expired.

    Raises ``FileNotFoundError`` if no cached token exists (the caller should
    run :func:`login` first). Fails fast; never logs token material.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as OAuthCredentials

    token_path = _token_path(config)
    if not token_path.exists():
        raise FileNotFoundError(
            f"no cached OAuth token at {token_path}; run 'cgc auth login' first"
        )

    creds = OAuthCredentials.from_authorized_user_file(str(token_path), CHAT_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_token(token_path, creds)
        else:
            raise ValueError(
                f"cached OAuth token at {token_path} is invalid and cannot be "
                "refreshed; run 'cgc auth login' again"
            )
    return creds


def login(config: Config) -> Credentials:
    """Run the installed-app OAuth flow and cache the resulting token.

    Returns the obtained credentials. Fails fast if the client secrets file is
    missing. The token is written with 0600 permissions and never logged.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_path = _require_client_file(config)
    token_path = _token_path(config)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), CHAT_SCOPES)
    creds = flow.run_local_server(port=0)
    _write_token(token_path, creds)
    return creds


def _write_token(token_path: Path, creds: Credentials) -> None:
    """Write the cached token to disk with owner-only permissions."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)


def _require_service_account_file(config: Config) -> Path:
    """Return the service-account key path, raising if absent or missing."""
    if not config.service_account_file:
        raise ValueError(
            "missing required config value 'service_account_file' "
            "(set CGC_SERVICE_ACCOUNT_FILE or add it to config.toml); this is the "
            "JSON key for the Chat app's service account used by 'cgc bootstrap' "
            "and 'cgc serve'"
        )
    sa_path = Path(config.service_account_file)
    if not sa_path.exists():
        raise FileNotFoundError(f"service account key file not found: {sa_path}")
    return sa_path


def load_app_credentials(
    config: Config,
    scopes: list[str] | None = None,
) -> ServiceAccountCredentials:
    """Load Google **service-account** (app) credentials for the Chat app.

    This is the app-auth path (NOT user OAuth): ``cgc bootstrap`` and
    ``cgc serve`` act as the Chat app itself. Fails fast with an actionable
    message if the service-account key file is missing. Never logs key material.
    """
    from google.oauth2 import service_account

    sa_path = _require_service_account_file(config)
    resolved_scopes = APP_SCOPES if scopes is None else scopes
    return service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=resolved_scopes
    )
