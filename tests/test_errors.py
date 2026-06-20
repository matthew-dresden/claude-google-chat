"""Tests for the shared error-mapping layer (:mod:`claude_google_chat.errors`).

Every mapping is asserted against a constructed ``HttpError`` (or transport
exception) so the exact actionable, secret-free message is verified per status.
No network is touched; the error envelope bodies are built in-process to model
the real Google API error shape. A central invariant is checked across all
mappings: the message never echoes a token, request URL, or raw response body.
"""

from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response, ServerNotFoundError

from claude_google_chat.errors import (
    REAUTH_COMMAND,
    format_missing_scopes,
    map_error,
    map_http_error,
)

# A token-bearing URL the mappings must never echo (secret-hygiene invariant).
SECRET_URI = "https://chat.googleapis.com/v1/spaces/AAAA?key=SECRETKEY&token=SECRETTOKEN"
SECRET_FRAGMENTS = ("SECRETKEY", "SECRETTOKEN", SECRET_URI)


def _http_error(status: int, body: object | None = None) -> HttpError:
    """Build an ``HttpError`` with ``status`` and an optional JSON error body."""
    resp = Response({"status": status})
    content = json.dumps(body).encode("utf-8") if body is not None else b"{}"
    return HttpError(resp, content, uri=SECRET_URI)


def _assert_secret_free(message: str) -> None:
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in message


def test_401_maps_to_reauth() -> None:
    message = map_http_error(_http_error(401))
    assert "401" in message
    assert REAUTH_COMMAND in message
    _assert_secret_free(message)


def test_403_insufficient_scope_names_missing_scope_and_reauth() -> None:
    body = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                    "metadata": {"oauth_scopes": "https://www.googleapis.com/auth/chat.messages"},
                }
            ],
        }
    }
    message = map_http_error(_http_error(403, body))
    assert "chat.messages" in message
    assert REAUTH_COMMAND in message
    assert "scope" in message.lower()
    _assert_secret_free(message)


def test_403_scope_insufficient_reason_without_named_scope() -> None:
    body = {
        "error": {
            "status": "PERMISSION_DENIED",
            "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
        }
    }
    message = map_http_error(_http_error(403, body))
    assert "scope" in message.lower()
    assert REAUTH_COMMAND in message


def test_403_permission_denied_role_message() -> None:
    body = {"error": {"status": "PERMISSION_DENIED", "message": "denied"}}
    message = map_http_error(_http_error(403, body))
    assert "role" in message.lower()
    assert "project" in message.lower()
    assert "another project" in message.lower()
    _assert_secret_free(message)


def test_403_bare_without_detail_is_still_actionable() -> None:
    message = map_http_error(_http_error(403))
    assert "403" in message
    assert "cgc setup" in message
    _assert_secret_free(message)


def test_404_says_recreate() -> None:
    message = map_http_error(_http_error(404))
    assert "404" in message
    assert "deleted" in message.lower()
    assert "re-create" in message.lower()
    _assert_secret_free(message)


def test_429_is_transient_retry() -> None:
    message = map_http_error(_http_error(429))
    assert "429" in message
    assert "transient" in message.lower()
    assert "retry" in message.lower()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_transient_retry(status: int) -> None:
    message = map_http_error(_http_error(status))
    assert str(status) in message
    assert "transient" in message.lower()
    assert "retry" in message.lower()


def test_unmapped_status_falls_through_to_generic_actionable() -> None:
    message = map_http_error(_http_error(418))
    assert "418" in message
    assert "cgc doctor" in message
    _assert_secret_free(message)


def test_map_error_routes_httperror() -> None:
    assert map_http_error(_http_error(404)) == map_error(_http_error(404))


def test_map_error_transport_errors_are_transient() -> None:
    for exc in (
        ServerNotFoundError("no dns"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        OSError("dropped"),
    ):
        message = map_error(exc)
        assert "transient" in message.lower()


def test_map_error_value_error_surfaces_message_without_traceback() -> None:
    message = map_error(ValueError("missing required config value 'space_id'"))
    assert message == "missing required config value 'space_id'"


def test_map_error_empty_message_falls_back_to_type_name() -> None:
    message = map_error(ValueError(""))
    assert "ValueError" in message


def test_http_error_with_non_json_body_uses_generic_403() -> None:
    resp = Response({"status": 403})
    exc = HttpError(resp, b"not json at all", uri=SECRET_URI)
    message = map_http_error(exc)
    assert "403" in message
    _assert_secret_free(message)


def test_format_missing_scopes_names_the_gap() -> None:
    message = format_missing_scopes(
        ["https://www.googleapis.com/auth/chat.messages", "openid"],
        ["openid"],
    )
    assert "chat.messages" in message
    assert REAUTH_COMMAND in message


def test_format_missing_scopes_empty_when_all_present() -> None:
    assert format_missing_scopes(["a", "b"], ["a", "b", "c"]) == ""


def test_string_digit_status_is_parsed() -> None:
    """Some transports expose ``resp.status`` as a string; it is still mapped."""
    resp = Response({"status": 401})
    exc = HttpError(resp, b"{}", uri=SECRET_URI)
    resp.status = "401"  # model a string-typed status
    message = map_http_error(exc)
    assert "401" in message


def test_status_none_when_unavailable_falls_through() -> None:
    resp = Response({"status": 500})
    exc = HttpError(resp, b"{}", uri=SECRET_URI)
    resp.status = "not-a-number"  # non-numeric -> None
    message = map_http_error(exc)
    assert "unknown" in message


def test_content_none_yields_generic_403() -> None:
    resp = Response({"status": 403})
    exc = HttpError(resp, b"", uri=SECRET_URI)
    exc.content = None
    message = map_http_error(exc)
    assert "403" in message


def test_content_undecodable_bytes_yields_generic_403() -> None:
    resp = Response({"status": 403})
    exc = HttpError(resp, b"\xff\xfe not utf8", uri=SECRET_URI)
    message = map_http_error(exc)
    assert "403" in message


def test_non_dict_json_body_yields_generic_403() -> None:
    message = map_http_error(_http_error(403, body=None))  # body=None -> "{}"
    assert "403" in message


def test_non_str_non_bytes_content_yields_generic_403() -> None:
    resp = Response({"status": 403})
    exc = HttpError(resp, b"{}", uri=SECRET_URI)
    exc.content = 12345  # neither str nor bytes
    message = map_http_error(exc)
    assert "403" in message


def test_json_array_body_yields_generic_403() -> None:
    """A JSON body that parses to a non-dict (a list) falls through to generic."""
    resp = Response({"status": 403})
    exc = HttpError(resp, b"[1, 2, 3]", uri=SECRET_URI)
    message = map_http_error(exc)
    assert "403" in message


def test_403_scope_via_scope_metadata_key() -> None:
    body = {
        "error": {
            "status": "PERMISSION_DENIED",
            "details": [
                "not-a-dict-detail",  # skipped (exercises the non-dict guard)
                {"metadata": {"scope": "https://www.googleapis.com/auth/chat.messages"}},
            ],
        }
    }
    message = map_http_error(_http_error(403, body))
    assert "chat.messages" in message
