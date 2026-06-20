# Setup Runbook — Zero to a Working Google Chat ↔ Claude Integration

This is the **complete, numbered, in-order runbook** for standing up the
`claude-google-chat` integration using **Google service-account (app) auth** for
the Chat API.

Design in one sentence: **Terraform** provisions the GCP infrastructure, a
Python **`cgc bootstrap`** command performs the API-level steps Terraform
cannot, and there is **exactly one** irreducible manual step — the Google Chat
API *Configuration* console page, for which **no API exists**.

> **Auth model:** the core path uses a **service account** (the Chat *app*
> identity). It does **not** use user OAuth and does **not** use incoming
> webhooks. The bot posts and reads as the app. Terraform creates the service
> account but does **not** download a key file; you authenticate as that account
> with Application Default Credentials (impersonation, or a key you obtain
> out-of-band) and `cgc bootstrap` wires the rest up.

Each step is tagged:

- **[AUTOMATED: terraform]** — `terraform` does it; you just run apply.
- **[AUTOMATED: cgc]** — the `cgc` CLI does it.
- **[MANUAL: you]** — a human (or Claude driving a browser) must click/approve.

Do the steps **in order**. Do not skip the verification steps — per project
policy, never assume success; always verify.

---

## What is automated vs manual (summary)

| # | Step | Who | How |
|---|------|-----|-----|
| 0 | Install prerequisites (gcloud, terraform, uv, GCP project) | **MANUAL: you** | package managers + `gcloud` |
| 1 | Authenticate gcloud / set ADC | **MANUAL: you** | `gcloud auth login` + `gcloud auth application-default login` |
| 2 | Configure Terraform variables | **MANUAL: you** | edit `terraform.tfvars` |
| 3 | `terraform init` | **AUTOMATED: terraform** | `terraform init` |
| 4 | `terraform apply` — enable APIs, create service account, Pub/Sub topic + subscription, IAM | **AUTOMATED: terraform** | `terraform apply` |
| 5 | Read Terraform outputs (service-account email, project id, Pub/Sub topic, config path) | **AUTOMATED: terraform** | `terraform output` |
| 6 | **Google Chat API → Configuration** page (app name, avatar, app auth = SA email, connection = Pub/Sub topic, visibility) | **MANUAL: you** | console clicks (no API exists) |
| 7 | Workspace-admin approval / app allowlisting (if required) | **MANUAL: you** | Admin console |
| 8 | `cgc bootstrap` — join/create the space, register the Workspace Events subscription, merge config | **AUTOMATED: cgc** | `cgc bootstrap` |
| 9 | Add the app to a Chat space | **MANUAL: you** | Chat client |
| 10 | `cgc serve` — run the integration | **AUTOMATED: cgc** | `cgc serve` |
| 11 | Verify a test message round-trips | **MANUAL: you** + **AUTOMATED: cgc** | post in Chat, observe `cgc` output |

**The single irreducible manual step is #6.** Everything else is automated or a
one-time prerequisite. Step 7 is conditional (only if your Workspace requires
admin approval for apps). Step 9 is a normal Chat client action.

---

## 0. Prerequisites — [MANUAL: you]

Install these once on the machine that will run the integration.

1. **A Google Cloud project** you own or can administer, and a **Google
   Workspace** account in the same organization (Google Chat apps require
   Workspace, not a consumer Gmail account).
2. **gcloud CLI** — https://cloud.google.com/sdk/docs/install
   ```bash
   gcloud version
   ```
3. **Terraform** (>= 1.5) — https://developer.hashicorp.com/terraform/install
   ```bash
   terraform version
   ```
4. **uv** — https://docs.astral.sh/uv/
   ```bash
   uv --version
   ```
5. **The `cgc` CLI** (this package):
   ```bash
   pipx install claude-google-chat
   cgc --version
   ```
   Or from a source checkout:
   ```bash
   git clone https://github.com/matthew-dresden/claude-google-chat
   cd claude-google-chat
   uv sync
   uv run cgc --version
   ```

**Verify (do not skip):** every command above must print a version with exit
code `0`. If any fails, fix it before continuing — the rest of the runbook
assumes all four tools are on your `PATH`.

---

## 1. Authenticate gcloud and set Application Default Credentials — [MANUAL: you]

Terraform authenticates to GCP using your **Application Default Credentials
(ADC)**. Set both your user login and ADC:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Replace `YOUR_PROJECT_ID` with your real project id (no placeholder is baked
into any artifact — the project id is an input).

**Verify:**

```bash
gcloud config get-value project          # prints YOUR_PROJECT_ID
gcloud auth application-default print-access-token >/dev/null && echo "ADC OK"
```

Both must succeed. If `ADC OK` does not print, re-run
`gcloud auth application-default login`.

---

## 2. Configure Terraform variables — [MANUAL: you]

From the Terraform directory in this repo (`terraform/`), create your variables
file from the example and edit it:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Set at minimum (these are **inputs**, never hardcoded in the code):

```hcl
project_id           = "YOUR_PROJECT_ID"
region               = "us-central1"        # your preferred region
service_account_name = "claude-chat-app"    # the Chat app identity
```

**Verify:** `terraform.tfvars` exists and contains your real `project_id`:

```bash
test -f terraform.tfvars && echo "tfvars present"
```

---

## 3. Initialize Terraform — [AUTOMATED: terraform]

```bash
terraform init
```

This downloads the Google provider and prepares the working directory.

**Verify:** the command exits `0` and prints `Terraform has been successfully
initialized!`.

---

## 4. Apply Terraform — [AUTOMATED: terraform]

```bash
terraform plan      # review what will be created
terraform apply     # type 'yes' to confirm
```

**What `terraform apply` creates / configures:**

- **Enables the required Google APIs** on the project:
  - **Google Chat API** (`chat.googleapis.com`)
  - **Cloud Pub/Sub API** (`pubsub.googleapis.com`)
  - **Google Workspace Events API** (`workspaceevents.googleapis.com`)
  - **IAM API** (`iam.googleapis.com`)
  - **Cloud Resource Manager API** (`cloudresourcemanager.googleapis.com`)
- **Creates the service account** that is the Chat **app identity**
  (e.g. `cgc-chat@YOUR_PROJECT_ID.iam.gserviceaccount.com`).
- **Creates the Pub/Sub topic + subscription** for event delivery and the IAM
  grants that let the Chat push system account publish and the app subscribe.
- **Renders a `config.toml`** to the cgc OS config path with the discovered
  values (project id, topic, cadence settings).

> **Note on credentials:** Terraform creates the service-account **identity** but
> does **not** download a key file — there is no key output. Authenticate as the
> service account with Application Default Credentials (impersonation, or a key
> you obtain out-of-band) and point cgc at those credentials via
> `CGC_SERVICE_ACCOUNT_FILE` in step 8.
>
> **Note on the boundary:** Terraform can enable the APIs and create the
> service-account identity, but it **cannot** fill in the Chat app
> *Configuration* page (step 6) — Google exposes **no API** for that screen.
> That is the single manual step this design cannot remove.

**Verify (do not skip):**

```bash
terraform apply     # must end with "Apply complete!" and a non-error exit code
```

If apply fails on an API-not-enabled error, re-run `terraform apply` once — API
enablement can take a moment to propagate, and Terraform is idempotent
(re-applying converges to the same declared state).

---

## 5. Read the Terraform outputs — [AUTOMATED: terraform]

You need these values for the manual console step and for `cgc bootstrap`:

```bash
terraform output service_account_email   # e.g. cgc-chat@PROJECT.iam.gserviceaccount.com
terraform output project_id              # YOUR_PROJECT_ID
terraform output pubsub_topic            # projects/PROJECT/topics/cgc-chat-events-...
terraform output config_file_path        # path of the rendered config.toml
terraform output -raw manual_next_steps  # the exact console steps + values to enter
```

Copy the **`service_account_email`** value (for the console in step 6) and the
**`pubsub_topic`** value (for the console connection setting in step 6 and for
`cgc bootstrap` in step 8).

**Verify:** the outputs print non-empty values:

```bash
test -n "$(terraform output -raw service_account_email)" && echo "SA email present"
test -n "$(terraform output -raw pubsub_topic)"          && echo "topic present"
```

---

## 6. Configure the Google Chat app — THE ONE MANUAL STEP — [MANUAL: you]

This is the **only** step with no API. You must do it in the console.

**Open the Chat API Configuration page** (substitute your real project id):

```
https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=YOUR_PROJECT_ID
```

That is the **Google Chat API → Configuration** tab. Fill it in exactly:

1. **App name** — e.g. `Claude` (this is what shows in the space).
2. **Avatar URL** — a public HTTPS image URL for the bot avatar.
3. **Description** — a short line, e.g. `Two-way ChatOps for Claude Code`.
4. **Functionality** — enable **"Receive 1:1 messages"** and **"Join spaces and
   group conversations"** so the app can be added to a space and read messages.
5. **Connection settings** — under **"App URL"**, select **"Cloud Pub/Sub"**
   and enter the **`pubsub_topic`** value from step 5 as the events topic
   (`projects/YOUR_PROJECT_ID/topics/cgc-chat-events-...`). This is the
   event-delivery path the Terraform module provisions; do **not** configure an
   HTTP endpoint URL.
6. **Authentication / App credentials** — set the app to authenticate **as the
   service account** and paste the **`service_account_email`** value from
   step 5 (the `...@YOUR_PROJECT_ID.iam.gserviceaccount.com` address).
7. **Visibility** — choose who can install the app:
   - **Specific people/groups** (recommended to start — add your own address),
     or
   - **Everyone in your organization** (only if your Workspace policy allows).
8. Click **Save**.

**Verify:** the page reloads showing **Status: LIVE — available to users** (or
your chosen visibility). If it still shows the app as not configured, re-open
the URL and confirm every field above is filled and saved.

---

## 7. Workspace-admin approval / allowlisting — [MANUAL: you] *(conditional)*

If your Google Workspace **restricts which Chat apps users may install**, a
**Workspace admin** must allow this app before anyone can add it to a space.

**Admin console:**

```
https://admin.google.com/  →  Apps  →  Google Workspace  →  Google Chat & Spaces
```

Then under **Chat apps** (or **"Configure Chat apps"**): add this app to the
**allowlist** / set it to **Allowed**, scoped to the org units you want.

- If you are **not** a Workspace admin, send the app name and project id to your
  admin and ask them to allowlist it.
- If your org has **no restriction** on Chat apps, **skip this step**.

**Verify:** an allowed test user can find the app by name in the Chat **"Find
apps"** dialog. If it does not appear, allowlisting is incomplete.

---

## 8. Bootstrap the CLI — [AUTOMATED: cgc]

`cgc bootstrap` does the API-level wiring Terraform left to the app layer:
authenticating as the service account, it **joins** the target Chat space (or
**creates** one from a display name), registers the Google **Workspace Events**
`message.created` subscription that delivers to the Pub/Sub topic, and merges the
discovered values into `config.toml`.

Provide the inputs from Terraform (all env-driven, nothing hardcoded). From the
`terraform/` directory you can feed the outputs straight in. `cgc bootstrap`
requires `service_account_file` **and** `pubsub_topic`, plus either a
`space_id` to join or a `space_display_name` to create:

```bash
export CGC_PROJECT_ID="$(terraform output -raw project_id)"
export CGC_PUBSUB_TOPIC="$(terraform output -raw pubsub_topic)"

# Point cgc at credentials for the service account from step 5
# (impersonation via ADC, or a key you obtained out-of-band):
export CGC_SERVICE_ACCOUNT_FILE="/path/to/service-account-credentials.json"

# Join an existing space (its id) OR create a new one (a display name):
export CGC_SPACE_ID="spaces/AAAA..."          # to join an existing space, or
# export CGC_SPACE_DISPLAY_NAME="Claude Ops"  # to create a new space

cgc bootstrap
```

`cgc bootstrap` will:

- Authenticate as the service account and **join** the configured space (or
  **create** one when only `space_display_name` is set). Fails fast with exact
  instructions if step 6 is incomplete (HTTP 403); a mistyped/inaccessible space
  id (HTTP 404) reports a distinct "space not found" error.
- Create the **Workspace Events `message.created` subscription** delivering to
  the **Pub/Sub topic** (idempotent — an existing subscription is reused).
- Merge the discovered `space_id`, subscription, and `pubsub_topic` into the
  user config (`config.toml` under the OS config dir — never in the repo).

**Verify (do not skip):**

```bash
cgc bootstrap        # must exit 0 and print the space, subscription, and topic
cgc config show      # secrets masked; confirms project_id + service_account_file + space_id
```

If `cgc bootstrap` reports it cannot authenticate as the Chat app, the most
common cause is that the **Configuration page (step 6) has not been saved with
the service-account email**, or a **Workspace allowlist (step 7)** still blocks
the app.

---

## 9. Add the app to a Chat space — [MANUAL: you]

In the **Google Chat** client:

1. Open or create the **space** you want Claude to use.
2. Click the space name → **Apps & integrations** → **Add apps**.
3. Search for your app by the **name you set in step 6** and **Add** it.
4. Copy the **space id**. The resource id looks like `spaces/AAAA...`.

> **Ordering:** if you set `CGC_SPACE_ID` in step 8, add the app to that space
> **before** running `cgc bootstrap` so it can join. Alternatively, set
> `CGC_SPACE_DISPLAY_NAME` instead and let `cgc bootstrap` create the space —
> then this step is just confirming the app is present.

Record the space id as an input:

```bash
export CGC_SPACE_ID="spaces/AAAA..."     # your real space id
```

**Verify:** the space membership list shows your app as a member.

---

## 10. Run the integration — [AUTOMATED: cgc]

Start the service. It authenticates as the service account, reads the configured
space via the Chat REST API, and surfaces inbound trigger-prefixed messages for
Claude while letting Claude post back as the app.

```bash
cgc serve
```

- Configuration comes from the environment / user config resolved in steps 8–9
  (`CGC_PROJECT_ID`, `CGC_SERVICE_ACCOUNT_FILE`, `CGC_SPACE_ID`, and the optional
  `CGC_TRIGGER_PREFIX`, `CGC_POLL_INTERVAL`, `CGC_LISTEN_TIMEOUT`).
- The process is stateless and logs to stdout (12-factor); stop it with
  `Ctrl-C` (graceful shutdown).

**Verify:** `cgc serve` starts without a fail-fast config error and logs that it
is watching your space. A missing required value exits non-zero and names the
key — set it and re-run.

---

## 11. Verify a test message round-trips — [MANUAL: you] + [AUTOMATED: cgc]

This is the end-to-end proof. With `cgc serve` running:

1. **Inbound (you → Claude):** in the Chat space, post a trigger-prefixed
   command, e.g.:
   ```
   claude: status
   ```
   (Use whatever `CGC_TRIGGER_PREFIX` you configured; the default is
   `claude:`.)

2. **Observe:** `cgc serve` emits a structured JSON line on stdout for that
   message — confirming the app **read** the space as the service account.

3. **Outbound (Claude → you):** send a status ping back as the app:
   ```bash
   cgc chat send --status success --text "round-trip verified"
   ```
   The message appears **in the space, posted by the app** — confirming the app
   **writes** as the service account.

**Verify:** you saw **both** directions — the inbound JSON line in the `cgc`
output **and** the outbound message rendered in the Chat space under the app's
name. If only one direction works, recheck step 6 (app must be authorized for
both receive and post) and step 9 (app must be a member of the space).

You now have a working Google Chat ↔ Claude integration.

---

## Teardown — `terraform destroy`

To remove everything Terraform created, in reverse:

1. **Stop the service** — `Ctrl-C` the `cgc serve` process.
2. **Remove the app from the space** (Chat client → space → **Apps &
   integrations** → remove the app). *Manual; no API.*
3. **Un-configure / disable the app** on the Chat API **Configuration** page
   (the same console URL from step 6) if you want it fully gone. *Manual.*
4. **Destroy the GCP infrastructure** — [AUTOMATED: terraform]:
   ```bash
   cd terraform
   terraform destroy     # type 'yes' to confirm
   ```
   This deletes the service account, the Pub/Sub topic + subscription, the
   rendered `config.toml`, and the IAM bindings Terraform created. (Whether the **APIs** are disabled on destroy depends on the
   `disable_on_destroy` setting in the Terraform config; leaving APIs enabled is
   harmless.)
5. **Remove local credentials / config** *(optional cleanup)*:
   ```bash
   # Remove any service-account credentials you obtained out-of-band, then:
   cgc config show       # confirm what remains; remove the config file if desired
   ```

**Verify:** `terraform destroy` ends with `Destroy complete!`, and
`gcloud iam service-accounts list --project YOUR_PROJECT_ID` no longer lists the
app service account.

---

## Troubleshooting quick reference

| Symptom | Most likely cause | Fix |
|---|---|---|
| `terraform apply` fails: API not enabled | API enablement still propagating | Re-run `terraform apply` (idempotent) |
| `cgc bootstrap` can't authenticate as the app | Step 6 Configuration not saved with the SA email | Re-open the console URL; set app auth = service-account email; **Save** |
| App not findable in Chat "Find apps" | Workspace allowlist (step 7) | Ask a Workspace admin to allow the app |
| `cgc serve` exits non-zero naming a config key | Required input missing | Set the named env var / config key and re-run |
| Inbound works, outbound doesn't (or vice-versa) | App not authorized for both, or not a space member | Recheck step 6 functionality + step 9 membership |

All failures are **fail-fast** with a clear, non-secret message naming what to
fix — never a silent fallback.

---

## See also

- [Installation](installation.md) — install paths and the build/release pipeline.
- [Configuration](configuration.md) — full config table, precedence, secret handling.
- [Architecture](architecture.md) — module responsibilities and data flow.
- [Usage](usage.md) — command reference and structured message examples.
