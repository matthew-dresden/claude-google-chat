# Outputs and the rendered config.toml for the claude-google-chat module.

locals {
  # Expand a leading "~" to the current user's home directory so the default
  # cgc config path works without the caller hardcoding an absolute home path.
  home_dir = pathexpand("~")
  resolved_config_path = (
    startswith(var.config_output_path, "~")
    ? "${local.home_dir}${trimprefix(var.config_output_path, "~")}"
    : var.config_output_path
  )

  # Event-driven resource identifiers, empty strings when the feature is off.
  pubsub_topic_id        = local.event_driven_enabled ? google_pubsub_topic.chat_events[0].id : ""
  pubsub_subscription_id = local.event_driven_enabled ? google_pubsub_subscription.chat_events[0].id : ""

  rendered_config = templatefile("${path.module}/templates/config.toml.tftpl", {
    project_id            = var.project_id
    service_account_email = google_service_account.chat_app.email
    pubsub_topic          = local.pubsub_topic_id
    space_id              = var.space_id
    trigger_prefix        = var.trigger_prefix
    poll_interval         = var.poll_interval
    listen_timeout        = var.listen_timeout
  })
}

# Write the rendered config.toml to the configured path with owner-only perms
# (it names the service account and project; treat it as sensitive config).
resource "local_file" "config" {
  content         = local.rendered_config
  filename        = local.resolved_config_path
  file_permission = "0600"
}

output "service_account_email" {
  description = "Email of the service account that is the Chat app identity. Enter this on the Chat app Configuration console page."
  value       = google_service_account.chat_app.email
}

output "service_account_id" {
  description = "Fully-qualified resource id of the Chat app service account."
  value       = google_service_account.chat_app.id
}

output "project_id" {
  description = "GCP project the infrastructure was provisioned in."
  value       = var.project_id
}

output "pubsub_topic" {
  description = "Pub/Sub topic id for Chat events, or empty when event-driven delivery is disabled."
  value       = local.pubsub_topic_id
}

output "pubsub_subscription" {
  description = "Pub/Sub subscription id the listener pulls from, or empty when event-driven delivery is disabled."
  value       = local.pubsub_subscription_id
}

output "config_file_path" {
  description = "Path of the rendered config.toml on disk."
  value       = local_file.config.filename
}

output "manual_next_steps" {
  description = "The irreducible human steps Terraform cannot automate (the Chat app Configuration console page has no API), plus the values to enter."
  value       = <<-EOT
    claude-google-chat: manual next steps (no API exists for these)

    Terraform has provisioned:
      - Project ................ ${var.project_id}
      - Chat app identity (SA) . ${google_service_account.chat_app.email}
      - Event-driven delivery .. ${local.event_driven_enabled ? "enabled" : "disabled"}
      - Pub/Sub topic .......... ${local.event_driven_enabled ? google_pubsub_topic.chat_events[0].id : "(disabled)"}
      - Pub/Sub subscription ... ${local.event_driven_enabled ? google_pubsub_subscription.chat_events[0].id : "(disabled)"}
      - Rendered config.toml ... ${local_file.config.filename}

    Complete setup by hand:

      1. Open the Google Chat API "Configuration" page for this project:
         https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${var.project_id}

      2. Under "App status", set the app to LIVE / available to users.

      3. Under "Connection settings", choose "App URL" -> "Cloud Pub/Sub"
         and enter this topic as the events topic:
           ${local.event_driven_enabled ? google_pubsub_topic.chat_events[0].id : "(enable_event_driven=true to provision a topic)"}

      4. Under "Permissions / Visibility", grant access to your Workspace
         domain or the specific people who will use the app.

      5. Make sure the app runs AS the service account identity:
           ${google_service_account.chat_app.email}
         (Run cgc with ADC for this SA, e.g. via GOOGLE_APPLICATION_CREDENTIALS
         to a key for this account, or workload identity / impersonation.)

      6. Add the Claude app to a Google Chat space, then capture the space id
         (form "spaces/XXXX"). If you did not set var.space_id, add it to:
           ${local_file.config.filename}
         under the `space_id` key.

      7. Verify end-to-end:
           cgc config show
           cgc listen --once     # should connect using the SA identity

    Useful gcloud references:
      gcloud config set project ${var.project_id}
      gcloud iam service-accounts describe ${google_service_account.chat_app.email}
      ${local.event_driven_enabled ? "gcloud pubsub subscriptions describe ${google_pubsub_subscription.chat_events[0].name} --project ${var.project_id}" : "# (event-driven disabled)"}
  EOT
}
