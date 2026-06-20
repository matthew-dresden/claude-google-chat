"""Tests for the pure bootstrap logic (topic, subscription body, classification)."""

from __future__ import annotations

import pytest

from claude_google_chat.bootstrap import (
    MESSAGE_CREATED_EVENT,
    build_subscription_body,
    is_not_configured_error,
    normalize_pubsub_topic,
)


def test_normalize_topic_from_bare_id_and_project() -> None:
    result = normalize_pubsub_topic("my-proj", "chat-events")
    assert result == "projects/my-proj/topics/chat-events"


def test_normalize_topic_passthrough_qualified() -> None:
    qualified = "projects/p1/topics/t1"
    assert normalize_pubsub_topic(None, qualified) == qualified


def test_normalize_topic_bare_id_requires_project() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_pubsub_topic(None, "chat-events")
    assert "project_id" in str(exc_info.value)


def test_normalize_topic_rejects_partial_path() -> None:
    with pytest.raises(ValueError):
        normalize_pubsub_topic("p1", "topics/t1")


def test_normalize_topic_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalize_pubsub_topic("p1", "")


def test_subscription_body_shape() -> None:
    body = build_subscription_body("spaces/AAAA", "projects/p/topics/t")
    assert body["targetResource"] == "//chat.googleapis.com/spaces/AAAA"
    assert body["eventTypes"] == [MESSAGE_CREATED_EVENT]
    assert body["notificationEndpoint"]["pubsubTopic"] == "projects/p/topics/t"
    assert body["payloadOptions"]["includeResource"] is True


def test_subscription_body_rejects_bad_space() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_subscription_body("AAAA", "projects/p/topics/t")
    assert "space" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "message",
    [
        "PERMISSION_DENIED: the caller does not have permission",
        "404 NOT_FOUND",
        "Chat app is not configured",
    ],
)
def test_is_not_configured_error_detects(message: str) -> None:
    assert is_not_configured_error(message) is True


def test_is_not_configured_error_ignores_unrelated() -> None:
    assert is_not_configured_error("INVALID_ARGUMENT: bad request body") is False
