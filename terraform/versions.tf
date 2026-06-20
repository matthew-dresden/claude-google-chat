# Provider and Terraform version constraints for the claude-google-chat module.
#
# No `credentials` block is configured on the google/google-beta providers on
# purpose: they pick up Application Default Credentials (ADC) or the
# GOOGLE_OAUTH_ACCESS_TOKEN environment variable at runtime, keeping the module
# environment-agnostic and secret-free (12-factor: config from the environment).

terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

# Credentials are intentionally omitted; ADC / GOOGLE_OAUTH_ACCESS_TOKEN is used.
provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
