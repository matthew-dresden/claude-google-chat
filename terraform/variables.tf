# Input variables for the claude-google-chat Terraform module.
#
# Every value is input-driven (no hardcoded project, region, names, or
# cadences). Only `project_id` is required; the rest carry documented defaults
# that mirror the cgc CLI defaults in src/claude_google_chat/config.py and
# messages.py so the rendered config.toml stays consistent with the package.

variable "project_id" {
  description = "GCP project ID in which to provision the Chat app infrastructure. The active credentials (ADC or GOOGLE_OAUTH_ACCESS_TOKEN) must have rights in this project."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must be a non-empty GCP project ID."
  }
}

variable "region" {
  description = "GCP region used for regional resources and as the provider default region."
  type        = string
  default     = "us-central1"
}

variable "service_account_id" {
  description = "Account ID (the local part of the email, before '@') for the service account that acts as the Chat app identity. Must match GCP's 6-30 char, lowercase-letters/digits/hyphens rule."
  type        = string
  default     = "cgc-chat"

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{4,28}[a-z0-9])$", var.service_account_id))
    error_message = "service_account_id must be 6-30 chars: start with a lowercase letter, contain only lowercase letters, digits, or hyphens, and not end with a hyphen."
  }
}

variable "enable_event_driven" {
  description = "When true, provision a Pub/Sub topic + subscription and grant the Chat/Workspace Events push service account publish rights, enabling event-driven delivery of Chat messages instead of pure polling."
  type        = bool
  default     = true
}

variable "trigger_prefix" {
  description = "Inbound command trigger prefix. Rendered into config.toml and consumed by the cgc listener (DEFAULT_TRIGGER_PREFIX in messages.py)."
  type        = string
  default     = "claude-command:"
}

variable "poll_interval" {
  description = "Listener poll cadence in seconds (DEFAULT_POLL_INTERVAL in config.py). Used as the fallback cadence even when event-driven delivery is enabled."
  type        = number
  default     = 2.0

  validation {
    condition     = var.poll_interval > 0
    error_message = "poll_interval must be a positive number of seconds."
  }
}

variable "listen_timeout" {
  description = "Listener idle timeout in seconds (DEFAULT_LISTEN_TIMEOUT in config.py). 0 means run forever."
  type        = number
  default     = 0

  validation {
    condition     = var.listen_timeout >= 0
    error_message = "listen_timeout must be 0 (run forever) or a positive number of seconds."
  }
}

variable "config_output_path" {
  description = "Filesystem path where the rendered config.toml is written. Defaults to the cgc OS config location on Linux/macOS. A leading '~' is expanded to the current user's home directory."
  type        = string
  default     = "~/.config/claude-google-chat/config.toml"

  validation {
    condition     = length(trimspace(var.config_output_path)) > 0
    error_message = "config_output_path must be a non-empty filesystem path."
  }
}

variable "space_id" {
  description = "Optional Google Chat space resource id (e.g. 'spaces/AAAA'). When known ahead of time it is rendered into config.toml; otherwise leave empty and fill it in after the Chat app is added to a space."
  type        = string
  default     = ""
}
