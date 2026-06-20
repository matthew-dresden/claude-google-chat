"""Shared error-mapping layer: Google API / transport failures → actionable text.

Onboarding, the doctor, the listener, and the chat transport all hit the same
remote-error surface (the Chat REST API and its OAuth/ADC credentials). Rather
than letting a raw ``HttpError`` traceback or a leaked request URL reach the
user, this module is the single source of truth (DRY) for mapping a failure to a
concise, **actionable** one-line message that names the exact fix command.

The mapping is **secret-free** by construction: it consults only the exception
type and (for an :class:`HttpError`) its HTTP status and a structured, allow-listed
view of the error body (``status``/``reason`` enum and any declared missing OAuth
scopes) — never the raw response body, request URL, or token material. This keeps
it safe to print in a financial-services context.

It composes with — and does not replace — :mod:`claude_google_chat.resilience`:
``resilience`` decides *whether* the poll loop absorbs or fails fast on an error,
while this module decides *what to tell the user* once a failure is surfaced.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

if TYPE_CHECKING:
    from collections.abc import Iterable

# The reauth command surfaced for credential/scope problems. Single source of
# truth so the wording can never drift across the messages below.
REAUTH_COMMAND = "cgc setup --reauth"

# HTTP statuses this layer maps to a specific, actionable remediation. A status
# outside this set falls through to the generic mapper.
HTTP_401_UNAUTHORIZED = 401
HTTP_403_FORBIDDEN = 403
HTTP_404_NOT_FOUND = 404
HTTP_429_TOO_MANY_REQUESTS = 429
SERVER_ERROR_FLOOR = 500  # 5xx == server-side / transient

# Google API error ``status`` enum spellings that distinguish a 403 caused by a
# missing-scope token from a 403 caused by a missing IAM role. Single source of
# truth for the two distinct 403 remediations.
PERMISSION_DENIED_STATUS = "PERMISSION_DENIED"


def _http_status(exc: HttpError) -> int | None:
    """Return the integer HTTP status of an ``HttpError``, or ``None``.

    Mirrors :func:`claude_google_chat.resilience.http_status` (kept local so this
    module stays independently importable) — reads only ``resp.status`` and never
    the response body.
    """
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


def _error_detail(exc: HttpError) -> dict[str, object]:
    """Return the structured ``error`` object from an ``HttpError`` body, or ``{}``.

    Parses only the Google API error envelope (``{"error": {...}}``) and returns
    the inner object so callers can read the allow-listed ``status``/``details``
    fields. Any parse failure yields ``{}`` (the generic remediation still
    applies) — the raw body text is never surfaced to the user.
    """
    content = getattr(exc, "content", None)
    if content is None:
        return {}
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    error = parsed.get("error")
    return error if isinstance(error, dict) else {}


def _api_status(detail: dict[str, object]) -> str | None:
    """Return the Google API ``error.status`` enum (e.g. ``PERMISSION_DENIED``)."""
    status = detail.get("status")
    return status if isinstance(status, str) and status else None


def _missing_scopes(detail: dict[str, object]) -> list[str]:
    """Return any OAuth scopes the API reports as missing, from the error details.

    Google surfaces an insufficient-scope 403 with an ``ErrorInfo`` detail whose
    ``metadata`` carries the required ``oauth_scopes``/``scope``. Reading only
    these declared, structured fields keeps the message secret-free while letting
    the doctor name the exact scope to re-request.
    """
    details = detail.get("details")
    if not isinstance(details, list):
        return []
    scopes: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        for key in ("oauth_scopes", "scope", "scopes"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                scopes.extend(part.strip() for part in value.split() if part.strip())
    # De-duplicate while preserving order so the message is stable.
    seen: dict[str, None] = {}
    for scope in scopes:
        seen.setdefault(scope, None)
    return list(seen)


def _is_insufficient_scope(detail: dict[str, object]) -> bool:
    """Return ``True`` if a 403 ``detail`` indicates an insufficient-scope token.

    A scope problem is recognised either by an explicit declared missing scope or
    by the ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` reason Google uses for that case.
    """
    if _missing_scopes(detail):
        return True
    details = detail.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("reason") == "ACCESS_TOKEN_SCOPE_INSUFFICIENT":
                return True
    return False


def _map_403(detail: dict[str, object]) -> str:
    """Map a 403 to the right remediation: insufficient scope vs. missing IAM role."""
    if _is_insufficient_scope(detail):
        scopes = _missing_scopes(detail)
        if scopes:
            named = ", ".join(scopes)
            return (
                f"insufficient OAuth scope: the cached token is missing {named}; "
                f"re-authenticate to grant it: run '{REAUTH_COMMAND}'"
            )
        return (
            "insufficient OAuth scope: the cached token lacks a required Chat scope; "
            f"re-authenticate to grant it: run '{REAUTH_COMMAND}'"
        )
    if _api_status(detail) == PERMISSION_DENIED_STATUS:
        return (
            "permission denied: your account needs the required role on this Google Cloud "
            "project (or the Chat API is not enabled on it). Grant the role / enable the API, "
            "or pick another project: run 'cgc setup'"
        )
    # A bare 403 with no recognisable detail: treat as a permission problem (the
    # most common cause) but keep the message actionable.
    return (
        "access forbidden (HTTP 403): your account lacks permission for this Chat resource "
        "or project. Check the project/role, or re-authenticate: run 'cgc setup'"
    )


def map_http_error(exc: HttpError) -> str:
    """Map a Chat API :class:`HttpError` to a concise, actionable, secret-free message.

    Covers the credential/permission/not-found/transient surface the onboarding
    wizard, doctor, listener, and chat transport all share:

    - **401 / invalid credentials** → re-authenticate (``cgc setup --reauth``).
    - **403 insufficient scope** → name the missing scope + re-auth.
    - **403 PERMISSION_DENIED** → you need a role on the project, or pick another.
    - **404** → the space/thread/webhook was not found (likely deleted); re-create it.
    - **429 / 5xx** → transient; retry.

    Any other status falls through to a generic, traceback-free message that
    still names the status so it is actionable.
    """
    status = _http_status(exc)
    if status == HTTP_401_UNAUTHORIZED:
        return f"invalid or expired credentials (HTTP 401); re-authenticate: run '{REAUTH_COMMAND}'"
    if status == HTTP_403_FORBIDDEN:
        return _map_403(_error_detail(exc))
    if status == HTTP_404_NOT_FOUND:
        return (
            "not found (HTTP 404): the space, thread, or webhook does not exist — "
            "it may have been deleted. Re-create it (e.g. run 'cgc setup' to reconfigure)"
        )
    if status == HTTP_429_TOO_MANY_REQUESTS:
        return "rate limited (HTTP 429): this is transient — retry shortly"
    if status is not None and status >= SERVER_ERROR_FLOOR:
        return f"Chat backend error (HTTP {status}): this is transient — retry shortly"
    status_text = str(status) if status is not None else "unknown"
    return (
        f"Chat API request failed (HTTP {status_text}); verify your configuration "
        "and run 'cgc doctor' to diagnose"
    )


def map_error(exc: BaseException) -> str:
    """Map any common Google API / transport failure to an actionable message.

    The single entry point used across the package. An :class:`HttpError` is
    routed through :func:`map_http_error`; transport-layer failures (a dropped
    connection, a socket timeout, an ``httplib2`` error) map to a transient
    retry message; everything else surfaces its message without a traceback.
    Never leaks request URLs, tokens, or raw response bodies.
    """
    if isinstance(exc, HttpError):
        return map_http_error(exc)
    if isinstance(exc, HttpLib2Error):
        return (
            "network error reaching the Chat API: this is transient — check connectivity and retry"
        )
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return (
            "network error reaching the Chat API: this is transient — check connectivity and retry"
        )
    # A non-transport error (e.g. a ValueError raised by our own fail-fast
    # config/validation path) already carries an actionable message; surface it
    # without a traceback.
    message = str(exc).strip()
    return message if message else f"{type(exc).__name__} occurred"


def format_missing_scopes(required: Iterable[str], present: Iterable[str]) -> str:
    """Return an actionable message naming scopes ``required`` but not ``present``.

    Used by the doctor's token-scope check and by setup's post-auth scope-drop
    guard so the "which scope is missing + re-auth" wording lives in one place
    (DRY). Returns an empty string when nothing is missing.
    """
    present_set = set(present)
    missing = [scope for scope in required if scope not in present_set]
    if not missing:
        return ""
    named = ", ".join(missing)
    return (
        f"the cached OAuth token is missing required Chat scope(s): {named}; "
        f"re-authenticate to grant them: run '{REAUTH_COMMAND}'"
    )
