"""Tests for transient-vs-fatal error classification used by the poll loop.

These assert the exact contract the resilient loop depends on: which errors are
absorbed (transient — log and continue), which fail fast (fatal auth/permission),
and that diagnostics never leak request URLs or response bodies (financial-grade
secret hygiene). All inputs are constructed in-process; no network is touched.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response, ServerNotFoundError

from claude_google_chat.resilience import (
    FATAL_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    diagnostic,
    http_status,
    is_fatal_error,
    is_transient_error,
)


def _http_error(status: int) -> HttpError:
    """Build a googleapiclient ``HttpError`` carrying ``status`` and a secret URL."""
    resp = Response({"status": status})
    content = b'{"error": {"message": "boom"}}'
    return HttpError(resp, content, uri="https://chat.googleapis.com/v1/spaces/AAAA?token=SECRET")


@pytest.mark.parametrize("status", sorted(TRANSIENT_HTTP_STATUSES))
def test_transient_http_statuses_are_transient_not_fatal(status: int) -> None:
    exc = _http_error(status)
    assert is_transient_error(exc) is True
    assert is_fatal_error(exc) is False


@pytest.mark.parametrize("status", sorted(FATAL_HTTP_STATUSES))
def test_fatal_http_statuses_are_fatal_not_transient(status: int) -> None:
    exc = _http_error(status)
    assert is_fatal_error(exc) is True
    assert is_transient_error(exc) is False


def test_unmapped_http_status_is_neither_transient_nor_fatal() -> None:
    """A 404 is neither retryable nor an auth failure: it propagates as-is."""
    exc = _http_error(404)
    assert is_transient_error(exc) is False
    assert is_fatal_error(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        ConnectionError("connection reset"),
        ConnectionResetError("reset by peer"),
        OSError("dropped"),
        ServerNotFoundError("Unable to find the server"),
    ],
)
def test_transport_layer_errors_are_transient(exc: BaseException) -> None:
    assert is_transient_error(exc) is True
    assert is_fatal_error(exc) is False


def test_value_error_is_not_transient() -> None:
    """A programming/validation error is not absorbed by the loop."""
    assert is_transient_error(ValueError("bad input")) is False
    assert is_fatal_error(ValueError("bad input")) is False


def test_http_status_parses_int_and_digit_string() -> None:
    assert http_status(_http_error(503)) == 503
    resp = Response({})
    resp.status = "429"  # type: ignore[assignment]
    err = HttpError(resp, b"{}", uri="https://x")
    assert http_status(err) == 429


def test_http_status_returns_none_for_unparseable_status() -> None:
    """A non-numeric ``resp.status`` yields no status (treated as unknown)."""
    resp = Response({})
    resp.status = "not-a-number"  # type: ignore[assignment]
    err = HttpError(resp, b"{}", uri="https://x")
    assert http_status(err) is None
    # An HttpError with no usable status is neither transient nor fatal.
    assert is_transient_error(err) is False
    assert is_fatal_error(err) is False


def test_diagnostic_uses_unknown_when_status_missing() -> None:
    resp = Response({})
    resp.status = "not-a-number"  # type: ignore[assignment]
    err = HttpError(resp, b"{}", uri="https://x")
    assert "unknown" in diagnostic(err)


def test_diagnostic_for_http_error_includes_status_but_no_secret() -> None:
    message = diagnostic(_http_error(503))
    assert "503" in message
    assert "SECRET" not in message
    assert "token" not in message
    assert "googleapis.com" not in message


def test_diagnostic_for_transport_error_names_type_only() -> None:
    message = diagnostic(TimeoutError("timed out"))
    assert "timeout" in message.lower()
    assert "timed out" not in message  # the message body is not echoed
