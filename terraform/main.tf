# Core resources for the claude-google-chat module.
#
# Provisions everything automatable for service-account (app) auth and
# event-driven Chat delivery in the caller's own GCP project:
#   - required Google APIs,
#   - the service account that is the Chat app identity,
#   - (optionally) a Pub/Sub topic + subscription and the IAM grant that lets
#     the Chat/Workspace Events push system service account publish to it.
#
# The irreducible manual step (the Chat app Configuration console page, which
# has no API) is surfaced via the `manual_next_steps` output, not done here.

locals {
  # APIs that must be enabled for the Chat app + event-driven path to work.
  required_services = [
    "chat.googleapis.com",
    "pubsub.googleapis.com",
    "workspaceevents.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]

  # Google's managed "push" service account used by the Chat API to publish
  # Workspace Events to a customer Pub/Sub topic. This is a well-known system
  # account; if Google changes it for your tenant, override the grant member
  # (see README "If the publisher service account differs").
  chat_push_service_account = "chat-api-push@system.gserviceaccount.com"

  # Resource names are derived from inputs plus a random suffix so repeated or
  # parallel applies in the same project do not collide.
  topic_name        = "cgc-chat-events-${random_id.suffix.hex}"
  subscription_name = "cgc-chat-events-sub-${random_id.suffix.hex}"

  # Whether event-driven resources exist (kept as a single source of truth).
  event_driven_enabled = var.enable_event_driven
}

# Enable the APIs the module and the runtime path depend on. disable_on_destroy
# is false so destroying this module never disables APIs that other workloads in
# the same project may rely on (fail-safe, non-destructive to siblings).
resource "google_project_service" "required" {
  for_each = toset(local.required_services)

  project = var.project_id
  service = each.value

  disable_on_destroy         = false
  disable_dependent_services = false
}

# Short random suffix to keep resource names unique per apply.
resource "random_id" "suffix" {
  byte_length = 4
}

# The service account that acts as the Chat app identity (service-account / app
# auth, not user OAuth). Its email is referenced on the Chat app Configuration
# console page (the manual step) and rendered into config.toml.
resource "google_service_account" "chat_app" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = "Claude Google Chat app identity"
  description  = "Service account used as the Chat app identity by claude-google-chat (managed by Terraform)."

  depends_on = [google_project_service.required]
}

# --- Event-driven delivery (optional) ---------------------------------------

# Pub/Sub topic that the Chat/Workspace Events API publishes message events to.
resource "google_pubsub_topic" "chat_events" {
  count = local.event_driven_enabled ? 1 : 0

  project = var.project_id
  name    = local.topic_name

  labels = {
    managed-by = "terraform"
    component  = "claude-google-chat"
  }

  depends_on = [google_project_service.required]
}

# Grant the Chat push system service account permission to publish events to the
# topic. Without this, Workspace Events subscriptions to Chat cannot deliver.
resource "google_pubsub_topic_iam_member" "chat_push_publisher" {
  count = local.event_driven_enabled ? 1 : 0

  project = var.project_id
  topic   = google_pubsub_topic.chat_events[0].name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${local.chat_push_service_account}"
}

# Subscription the cgc listener pulls events from. ack_deadline and retention
# are explicit (no reliance on provider defaults) so behavior is reproducible.
resource "google_pubsub_subscription" "chat_events" {
  count = local.event_driven_enabled ? 1 : 0

  project = var.project_id
  name    = local.subscription_name
  topic   = google_pubsub_topic.chat_events[0].id

  ack_deadline_seconds       = 30
  message_retention_duration = "600s"
  retain_acked_messages      = false

  expiration_policy {
    ttl = "" # never expires while in use
  }

  labels = {
    managed-by = "terraform"
    component  = "claude-google-chat"
  }
}

# Allow the Chat app service account to consume from the subscription, so the
# listener authenticating as that identity can pull events (least privilege:
# subscriber only, scoped to this subscription).
resource "google_pubsub_subscription_iam_member" "app_subscriber" {
  count = local.event_driven_enabled ? 1 : 0

  project      = var.project_id
  subscription = google_pubsub_subscription.chat_events[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.chat_app.email}"
}
