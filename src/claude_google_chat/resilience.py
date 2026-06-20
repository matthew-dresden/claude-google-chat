"""Transient-vs-fatal error classification for the poll loop.

A long-running ``cgc listen`` / ``cgc serve`` process must survive the routine
turbulence of a remote API: a dropped connection, a socket timeout, or a Chat
API ``429``/``5xx``. Today any such error propagates out of the poll loop and
kills the listener. This module is the single source of truth for deciding which
errors are **transient** (log a concise, secret-free diagnostic and continue
polling) and which are **fatal** (auth/permission failures that will never
self-heal and must fail fast).

Kept pure and dependency-light so it can be unit-tested in isolation and reused
by both loop callers (DRY).
"""

from __future__ import annotations

import socket

from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

# Chat API HTTP statuses that represent a transient/retryable condition: request
# timeout, rate limiting, and the standard server-side 5xx family. An error with
# one of these statuses is logged and the loop continues.
TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# HTTP statuses that are fatal for an unattended loop: the credentials are
# missing, invalid, or lack permission. Retrying forever would never succeed, so
# the loop fails fast with an actionable message instead.
FATAL_HTTP_STATUSES: frozenset[int] = frozenset({401, 403})


def http_status(exc: HttpError) -> int | None:
    """Return the HTTP status of an ``HttpError``, or ``None`` if unavailable."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


def is_fatal_error(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a fatal auth/permission failure (fail fast).

    Only an :class:`HttpError` carrying a status in :data:`FATAL_HTTP_STATUSES`
    is treated as fatal here; everything else is decided by
    :func:`is_transient_error`.
    """
    if isinstance(exc, HttpError):
        return http_status(exc) in FATAL_HTTP_STATUSES
    return False


def is_transient_error(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a transient error the loop should survive.

    Transient errors are network/transport hiccups and retryable HTTP statuses:

    - ``socket.timeout`` / :class:`TimeoutError` (the same object on modern
      Python) — a stalled read against the Chat API.
    - :class:`ConnectionError` / :class:`OSError` — a dropped or refused
      connection at the socket layer.
    - :class:`httplib2.HttpLib2Error` — the transport layer used by the Google
      API client.
    - :class:`googleapiclient.errors.HttpError` whose status is in
      :data:`TRANSIENT_HTTP_STATUSES` (``408``/``429``/``5xx``).

    A fatal :class:`HttpError` (``401``/``403``) is explicitly **not** transient
    so it fails fast via :func:`is_fatal_error`.
    """
    if isinstance(exc, HttpError):
        return http_status(exc) in TRANSIENT_HTTP_STATUSES
    if isinstance(exc, HttpLib2Error):
        return True
    # socket.timeout is an alias of TimeoutError on modern Python; OSError covers
    # ConnectionError and the broader dropped-connection family.
    if isinstance(exc, (socket.timeout, TimeoutError, OSError)):
        return True
    return False


def diagnostic(exc: BaseException) -> str:
    """Return a concise, secret-free one-line diagnostic for ``exc``.

    Uses the exception type and (for an :class:`HttpError`) its status only —
    never the response body or request URL, which may carry tokens — so the
    message is safe to write to stderr in a financial-services context.
    """
    if isinstance(exc, HttpError):
        status = http_status(exc)
        status_text = str(status) if status is not None else "unknown"
        return f"transient Chat API error: HTTP {status_text}"
    return f"transient poll error: {type(exc).__name__}"
