# Installation

`claude-google-chat` ships two installable artifacts from a single codebase:

1. A **Claude Code plugin** (slash commands + a skill) installable from a marketplace.
2. A **Python CLI** (`cgc`) that the plugin commands shell out to.

Install **both**: the plugin gives you `/claude-google-chat:*` commands inside Claude Code, and the CLI does the actual work of talking to Google Chat.

---

## Prerequisites

- **Python 3.11 or newer** (the CLI uses the stdlib `tomllib` module).
- **[uv](https://docs.astral.sh/uv/)** (recommended) or `pipx`/`pip` to install the CLI.
- **A Google Chat space** you can post to and (for inbound reading) a Google Cloud project.
- **Claude Code** (for the plugin half).

---

## 1. Install the CLI (Python)

### uv (recommended)

```bash
uv tool install claude-google-chat        # from PyPI once published
```

From source:

```bash
git clone https://github.com/matthew-dresden/claude-google-chat
cd claude-google-chat
uv sync
uv run cgc --help
```

### pipx

```bash
pipx install claude-google-chat
cgc --help
```

### pip

```bash
pip install claude-google-chat
cgc --help
```

Verify:

```bash
cgc --version
```

---

## 2. Install the Claude Code plugin

Inside Claude Code:

```
/plugin marketplace add matthew-dresden/claude-google-chat
/plugin install claude-google-chat@claude-google-chat
```

The marketplace name and the plugin name are both `claude-google-chat`, so the install selector is `claude-google-chat@claude-google-chat`.

After installation you have these commands:

- `/claude-google-chat:chat-setup` — interactive configuration helper.
- `/claude-google-chat:chat-send` — send a structured status ping.
- `/claude-google-chat:chat-listener` — start the inbound listener.

And a skill:

- `/claude-google-chat:google-chat` — documents the ChatOps protocol so Claude can read and produce structured messages.

The commands invoke the `cgc` CLI. If `cgc` is not on your `PATH`, `/claude-google-chat:chat-setup` prints the install commands from step 1 and stops.

---

## 3. Google Cloud setup

You need two things from Google: an **incoming webhook** (for outbound sends) and, if you want inbound reading, an **OAuth client**.

### 3a. Create an incoming webhook (outbound, no OAuth)

1. Open the target **Google Chat space**.
2. Open the space name menu → **Apps & integrations** → **Manage webhooks** (this requires that incoming webhooks are enabled for your Workspace).
3. Create a webhook, give it a name, and copy the generated **webhook URL**. It looks like:

   ```
   https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=...
   ```

4. Store it (never commit it):

   ```bash
   cgc config set webhook_url "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
   ```

   or export it:

   ```bash
   export CGC_WEBHOOK_URL="https://chat.googleapis.com/v1/spaces/AAAA/messages?key=...&token=..."
   ```

Send-only operation needs nothing more.

### 3b. Create an OAuth client (inbound read/listen)

Reading inbound commands uses the Google Chat REST API with OAuth user credentials.

1. In the [Google Cloud Console](https://console.cloud.google.com/), select or create a project.
2. Enable the **Google Chat API** for the project.
3. Configure the **OAuth consent screen** (internal is fine for a single Workspace).
4. Create an **OAuth client ID** of type **Desktop app**. Download the client secrets JSON.
5. Point the CLI at it:

   ```bash
   cgc config set oauth_client_file "/path/to/oauth_client.json"
   cgc config set space_id "spaces/AAAA"
   ```

6. Complete the installed-app OAuth flow once; the resulting user token is cached locally (with restrictive file permissions):

   ```bash
   cgc auth login
   ```

The OAuth scope requested is `https://www.googleapis.com/auth/chat.messages`. Outbound sends still use the webhook; OAuth is only required for read/listen/delete.

---

## 4. Verify

```bash
cgc config show                                  # masks secrets
cgc chat send --status info --text "hello from claude-google-chat"
cgc listen --once                                # drains pending messages and exits
```

A successful send returns an HTTP 2xx. If anything required is missing, the CLI fails fast with a clear message naming the missing key — it never silently falls back to a default for a secret.

---

## Next steps

- [Configuration](configuration.md) — full config reference, precedence, secret handling.
- [Usage](usage.md) — command reference and structured message examples.
- [Architecture](architecture.md) — how the pieces fit together.
