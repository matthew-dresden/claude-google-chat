"""Service-account (app) bootstrap for the Google Chat ChatOps integration.

``cgc bootstrap`` performs the API-level setup steps that Terraform cannot do
(because no Terraform provider exposes them), using **service-account / app
auth** — not user OAuth:

1. Ensure the Chat app is a member of the target space. It either *joins* an
   existing space (``spaces.members.create``) or *creates* a new space
   (``spaces.create``) when only a display name is configured.
2. Create a Google **Workspace Events** subscription for
   ``google.workspace.chat.message.v1.created`` on that space, delivering to the
   configured Pub/Sub topic (the topic itself is provisioned by Terraform).
3. Merge the discovered values (space id, subscription name, topic) into
   ``config.toml`` so subsequent ``cgc serve`` runs are configured.

The single irreducible manual step is the Chat app **Configuration** console
page (no API exists for it). If the app is not yet configured, every Chat API
call fails with ``PERMISSION_DENIED``/``NOT_FOUND``; this module detects that
and **fails fast** with an exact, actionable instruction instead of a raw
stack trace.

All failures exit non-zero with a clear, non-secret message. No fallbacks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_google_chat.config import Config, merge_and_write_config

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

# Event type emitted by Google Chat when a message is created in a space.
MESSAGE_CREATED_EVENT = "google.workspace.chat.message.v1.created"

# Chat space resource ids look like ``spaces/AAAA...``.
_SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")

# Fully-qualified Pub/Sub topic, e.g. ``projects/p/topics/t``.
_TOPIC_RE = re.compile(r"^projects/[^/]+/topics/[^/]+$")

# Substrings in Chat API errors that indicate the manual Configuration step is
# still pending (the app has no bot identity / is not authorized for Chat yet).
_NOT_CONFIGURED_MARKERS = (
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "is not configured",
    "Chat app",
    "caller does not have permission",
)


class ChatAppNotConfiguredError(RuntimeError):
    """Raised when the manual Chat app Configuration step is not yet done."""


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of a bootstrap run (pure data; no I/O)."""

    space_id: str
    created_space: bool
    joined_space: bool
    subscription_name: str
    pubsub_topic: str
    config_path: str


def normalize_pubsub_topic(project_id: str | None, topic: str) -> str:
    """Return a fully-qualified Pub/Sub topic resource name (pure, no I/O).

    Accepts either a bare topic id (``my-topic``) plus a project id, or an
    already-qualified ``projects/<p>/topics/<t>`` string. Fails fast with a
    clear message if a bare id is given without a project id, or if the result
    is malformed. This keeps topic construction DRY and unit-testable.
    """
    if not topic:
        raise ValueError("pubsub_topic must be a non-empty topic id or resource name")
    if topic.startswith("projects/"):
        qualified = topic
    else:
        if "/" in topic:
            raise ValueError(
                f"invalid pubsub_topic {topic!r}; expected a bare topic id or a "
                "'projects/<project>/topics/<topic>' resource name"
            )
        if not project_id:
            raise ValueError(
                "project_id is required to qualify the bare pubsub_topic "
                f"{topic!r} (set CGC_PROJECT_ID or pass a full "
                "'projects/<project>/topics/<topic>' value)"
            )
        qualified = f"projects/{project_id}/topics/{topic}"
    if not _TOPIC_RE.match(qualified):
        raise ValueError(
            f"invalid Pub/Sub topic {qualified!r}; expected 'projects/<project>/topics/<topic>'"
        )
    return qualified


def build_subscription_body(space_id: str, pubsub_topic: str) -> dict[str, Any]:
    """Build the Workspace Events subscription request body (pure, no I/O).

    Subscribes to ``message.created`` on ``space_id`` and routes events to the
    Pub/Sub ``pubsub_topic``. Validates the space id form, failing fast.
    """
    if not _SPACE_RE.match(space_id):
        raise ValueError(f"invalid space id {space_id!r}; expected form 'spaces/<id>'")
    return {
        "targetResource": f"//chat.googleapis.com/{space_id}",
        "eventTypes": [MESSAGE_CREATED_EVENT],
        "notificationEndpoint": {"pubsubTopic": pubsub_topic},
        "payloadOptions": {"includeResource": True},
    }


def is_not_configured_error(message: str) -> bool:
    """Return True if a Chat API error message implies the app is unconfigured.

    Pure string classification so the detection rule is testable without the
    network. Used to convert opaque API failures into the actionable
    "configure the Chat app" instruction.
    """
    return any(marker in message for marker in _NOT_CONFIGURED_MARKERS)


def _not_configured_instructions(config: Config) -> str:
    """Return the exact manual steps to finish Chat app configuration."""
    project = config.project_id or "<your-gcp-project>"
    console_url = (
        "https://console.cloud.google.com/apis/api/chat.googleapis.com/"
        f"hangouts-chat?project={project}"
    )
    return (
        "The Google Chat app is not configured yet — this is the one manual "
        "step no API can automate.\n"
        "Do this once, then re-run 'cgc bootstrap':\n"
        f"  1. Open {console_url}\n"
        "  2. On the 'Configuration' tab, set:\n"
        "       - App status: LIVE\n"
        "       - App name / avatar / description\n"
        "       - Functionality: 'Receive 1:1 messages' and "
        "'Join spaces and group conversations'\n"
        "       - Connection settings: choose 'Google Workspace Events API + "
        "Pub/Sub' (no HTTP endpoint required)\n"
        "       - Visibility: make the app available to the target users/space\n"
        "  3. Ensure the service account in CGC_SERVICE_ACCOUNT_FILE is the "
        "app's service account.\n"
        "Until the Configuration tab is saved, the Chat API rejects every call "
        "with PERMISSION_DENIED/NOT_FOUND."
    )


def _build_chat_service(config: Config) -> Resource:
    """Build a Chat API client with service-account (app) credentials."""
    from claude_google_chat.chat import build_app_service

    return build_app_service(config)


def _build_events_service(config: Config) -> Resource:
    """Build a Google Workspace Events API client with app credentials."""
    from googleapiclient.discovery import build

    from claude_google_chat.auth import APP_SCOPES, load_app_credentials

    creds = load_app_credentials(config, scopes=APP_SCOPES)
    return build("workspaceevents", "v1", credentials=creds, cache_discovery=False)


def _ensure_space(config: Config, chat: Resource) -> tuple[str, bool, bool]:
    """Ensure the app is in a space; return (space_id, created, joined).

    If ``space_id`` is configured, add the app as a member of that space
    (idempotent: an already-joined space is treated as success). Otherwise,
    create a new space named ``space_display_name``.
    """
    from googleapiclient.errors import HttpError

    if config.space_id:
        if not _SPACE_RE.match(config.space_id):
            raise ValueError(f"invalid space id {config.space_id!r}; expected form 'spaces/<id>'")
        try:
            chat.spaces().members().create(
                parent=config.space_id,
                body={"member": {"name": "users/app", "type": "BOT"}},
            ).execute()
            return config.space_id, False, True
        except HttpError as exc:
            text = str(exc)
            if exc.resp is not None and exc.resp.status == 409:
                # Already a member — idempotent success.
                return config.space_id, False, False
            if is_not_configured_error(text):
                raise ChatAppNotConfiguredError(_not_configured_instructions(config)) from exc
            raise

    if not config.space_display_name:
        raise ValueError(
            "neither 'space_id' nor 'space_display_name' is configured; set one "
            "(CGC_SPACE_ID to join an existing space, or CGC_SPACE_DISPLAY_NAME "
            "to create a new one)"
        )
    try:
        created = (
            chat.spaces()
            .create(body={"displayName": config.space_display_name, "spaceType": "SPACE"})
            .execute()
        )
    except HttpError as exc:
        if is_not_configured_error(str(exc)):
            raise ChatAppNotConfiguredError(_not_configured_instructions(config)) from exc
        raise
    space_id = created.get("name", "")
    if not space_id:
        raise RuntimeError(
            "Chat API returned a created space without a resource name; cannot continue"
        )
    return space_id, True, False


def _create_subscription(config: Config, events: Resource, space_id: str, topic: str) -> str:
    """Create the message.created Workspace Events subscription; return its name.

    Idempotent: an existing subscription (HTTP 409 ALREADY_EXISTS) is treated as
    success and its target topic is reported back via the configured topic.
    """
    from googleapiclient.errors import HttpError

    body = build_subscription_body(space_id, topic)
    try:
        result = events.subscriptions().create(body=body).execute()
    except HttpError as exc:
        text = str(exc)
        if exc.resp is not None and exc.resp.status == 409:
            return f"(existing subscription for {space_id})"
        if is_not_configured_error(text):
            raise ChatAppNotConfiguredError(_not_configured_instructions(config)) from exc
        raise
    return str(result.get("name", ""))


def bootstrap(config: Config) -> BootstrapResult:
    """Run the full service-account bootstrap and merge results into config.

    Steps (each fail-fast):
        1. Resolve/validate the Pub/Sub topic from ``project_id`` + ``pubsub_topic``.
        2. Join or create the target Chat space (app auth).
        3. Create the Workspace Events ``message.created`` subscription → topic.
        4. Merge ``space_id`` and ``pubsub_topic`` into ``config.toml``.

    Raises:
        ChatAppNotConfiguredError: if the manual Chat app Configuration step is
            not done yet — carrying exact, actionable instructions.
        ValueError / RuntimeError: for missing config or malformed API results.
    """
    if not config.pubsub_topic:
        raise ValueError(
            "missing required config value 'pubsub_topic' (set CGC_PUBSUB_TOPIC or "
            "add it to config.toml); this is the Pub/Sub topic Terraform created "
            "for Chat events"
        )
    topic = normalize_pubsub_topic(config.project_id, config.pubsub_topic)

    chat = _build_chat_service(config)
    space_id, created, joined = _ensure_space(config, chat)

    events = _build_events_service(config)
    subscription_name = _create_subscription(config, events, space_id, topic)

    config_path = merge_and_write_config({"space_id": space_id, "pubsub_topic": topic})

    return BootstrapResult(
        space_id=space_id,
        created_space=created,
        joined_space=joined,
        subscription_name=subscription_name,
        pubsub_topic=topic,
        config_path=str(config_path),
    )
