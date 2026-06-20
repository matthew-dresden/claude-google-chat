# claude-google-chat Terraform module

Provisions everything **automatable** for `claude-google-chat` in **your own GCP
project**, using **service-account (app) auth** for the Google Chat API (not user
OAuth, not incoming webhooks for the core path). It enables the required APIs,
creates the service account that is the Chat app identity, and — for the
event-driven path — creates a Pub/Sub topic + subscription and the IAM grant
that lets the Chat/Workspace Events push system account publish to it.

The one step no API can do — the **Chat app Configuration console page** — is
emitted as the `manual_next_steps` output for you to complete by hand.

This module pairs with the `cgc` CLI: it renders a `config.toml` (matching the
schema in `src/claude_google_chat/config.py`) to the cgc OS config path.

---

## What it creates

| Resource | Purpose |
|---|---|
| `google_project_service` (x5) | Enables `chat`, `pubsub`, `workspaceevents`, `iam`, `cloudresourcemanager` APIs (`disable_on_destroy = false`). |
| `google_service_account` | The Chat **app identity** (service-account auth). |
| `random_id` | Suffix keeping Pub/Sub resource names unique per apply. |
| `google_pubsub_topic` *(if `enable_event_driven`)* | Topic Chat/Workspace Events publishes message events to. |
| `google_pubsub_topic_iam_member` *(if `enable_event_driven`)* | Grants `roles/pubsub.publisher` to the Chat push system account on the topic. |
| `google_pubsub_subscription` *(if `enable_event_driven`)* | Subscription the listener pulls from. |
| `google_pubsub_subscription_iam_member` *(if `enable_event_driven`)* | Grants the app SA `roles/pubsub.subscriber` on the subscription. |
| `local_file` | Renders `config.toml` to `config_output_path`. |

---

## Authentication

The `google` / `google-beta` providers are configured **without a `credentials`
block** on purpose. They use whatever ambient credentials are present, so the
module stays environment-agnostic and secret-free:

```bash
# Application Default Credentials
gcloud auth application-default login

# or a short-lived access token
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)"
```

The credentials must be able to manage services, service accounts, and Pub/Sub
in `project_id`.

---

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: set project_id (and optionally space_id, etc.)

terraform init
terraform plan
terraform apply

# read the manual steps Terraform cannot automate
terraform output -raw manual_next_steps
```

After `apply`, complete the **Chat app Configuration console page** as printed in
`manual_next_steps` (set the app live, point its events at the Pub/Sub topic,
grant visibility, and run the app as the service account). Then add the app to a
Chat space and, if you did not set `space_id`, drop the resulting `spaces/XXXX`
id into the rendered `config.toml`.

---

## Inputs

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `project_id` | string | — | **yes** | GCP project to provision into. |
| `region` | string | `us-central1` | no | Region for regional resources / provider default. |
| `service_account_id` | string | `cgc-chat` | no | Account id (before `@`) for the Chat app identity SA. |
| `enable_event_driven` | bool | `true` | no | Provision Pub/Sub topic + subscription + IAM for event delivery. |
| `trigger_prefix` | string | `claude-command:` | no | Inbound command trigger prefix (rendered into config). |
| `poll_interval` | number | `2.0` | no | Listener poll cadence (seconds). |
| `listen_timeout` | number | `0` | no | Listener idle timeout (seconds); `0` = run forever. |
| `config_output_path` | string | `~/.config/claude-google-chat/config.toml` | no | Where the rendered `config.toml` is written (leading `~` expanded). |
| `space_id` | string | `""` | no | Optional Chat space id (`spaces/AAAA`); add later if unknown. |
| `subscription_ack_deadline_seconds` | number | `30` | no | Pub/Sub subscription ack deadline (10–600s). |
| `subscription_message_retention_duration` | string | `"600s"` | no | How long Pub/Sub retains unacked Chat events (seconds duration string). |
| `chat_push_service_account` | string | `chat-api-push@system.gserviceaccount.com` | no | Chat/Workspace Events push SA granted publish rights; override per tenant if it differs. |

---

## Outputs

| Output | Description |
|---|---|
| `service_account_email` | Email of the Chat app identity SA (enter on the console page). |
| `service_account_id` | Fully-qualified SA resource id. |
| `project_id` | Project provisioned into. |
| `pubsub_topic` | Topic id (empty if event-driven disabled). |
| `pubsub_subscription` | Subscription id (empty if event-driven disabled). |
| `config_file_path` | Path of the rendered `config.toml`. |
| `manual_next_steps` | The human-only Chat app console steps + the values to enter. |

---

## If the publisher service account differs

The module grants `roles/pubsub.publisher` to the well-known Chat push system
account:

```
chat-api-push@system.gserviceaccount.com
```

This is the documented account the Chat API uses to publish Workspace Events to
a customer Pub/Sub topic. If your tenant uses a different push identity, the
console may report a publish-permission error. In that case, override the input
variable in `terraform.tfvars` (no module-source edit required):

```hcl
chat_push_service_account = "the-identity-your-console-reports@system.gserviceaccount.com"
```

Then re-`apply` and re-test. The grant
(`google_pubsub_topic_iam_member.chat_push_publisher`) is the only place that
account is referenced.

---

## Destroy

```bash
terraform destroy
```

APIs are **not** disabled on destroy (`disable_on_destroy = false`) so sibling
workloads in the same project are unaffected. The service account, Pub/Sub
resources, and the rendered `config.toml` are removed.
