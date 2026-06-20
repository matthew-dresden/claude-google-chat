---
description: Configure the Google Chat ChatOps integration (webhook URL, space, OAuth, trigger prefix). Run this once before using chat-send or chat-listener.
argument-hint: "[--space <spaceId>]"
allowed-tools: Bash
disable-model-invocation: true
---

# Configure Google Chat ChatOps

Walk the user through configuring the `claude-google-chat` integration. This is a
one-time setup that must be completed before `/claude-google-chat:chat-send` or
`/claude-google-chat:chat-listener` will work. Configuration is written to the OS
user config directory by the `cgc` CLI — **never to this repository or the working
directory**.

Arguments passed to this command: `$ARGUMENTS`

Follow these steps in order. **Stop and report** the moment any step fails — do not
continue past a failure, and do not invent fallback behavior.

## 1. Verify the `cgc` CLI is installed

Run:

```bash
cgc --version
```

If the command is not found, the CLI is not installed. Print the following install
options and **stop** (do not attempt to proceed without `cgc`):

```bash
# pipx (recommended)
pipx install claude-google-chat

# or uv tool
uv tool install claude-google-chat

# or pip
pip install claude-google-chat

# or from source
git clone https://github.com/matthew-dresden/claude-google-chat
cd claude-google-chat
uv sync
uv run cgc --help
```

## 2. Show current configuration

Initialize (idempotent) and display the current config so the user can see which
keys are required and which are already set:

```bash
cgc config init
cgc config show
```

`cgc config show` masks secrets (the webhook token and cached token contents are
redacted). The required keys are:

- **`webhook_url`** (`CGC_WEBHOOK_URL`) — required for sending status pings.
- **`space_id`** (`CGC_SPACE_ID`) — required for reading/listening, e.g. `spaces/AAAA`.
- **`oauth_client_file`** (`CGC_OAUTH_CLIENT_FILE`) — required for reading/listening.
- **`trigger_prefix`** (`CGC_TRIGGER_PREFIX`) — optional, default `claude-command:`.

## 3. Create the Google Chat incoming webhook (outbound)

Direct the user to create an incoming webhook for the target space:

1. Open Google Chat and go to the space you want Claude to post into.
2. Open the space menu and choose **Apps & integrations → Webhooks → Add webhooks**.
3. Name it (for example `claude-code`) and copy the generated webhook URL.

Store it (this writes to the user config dir, never the repo):

```bash
cgc config set webhook_url "<PASTE_WEBHOOK_URL>"
```

The webhook is send-only and requires no OAuth.

## 4. Create the OAuth client (inbound — optional)

Inbound reading/listening uses the Google Chat REST API and requires OAuth user
credentials. If the user only wants outbound pings, skip to step 6.

1. In the Google Cloud Console, enable the **Google Chat API** for the project.
2. Configure the OAuth consent screen.
3. Create an **OAuth client ID** of type **Desktop app** and download the client
   secrets JSON file.
4. Note the space resource id (`spaces/...`).

Store the values:

```bash
cgc config set oauth_client_file "<PATH_TO_CLIENT_SECRETS_JSON>"
cgc config set space_id "<spaces/XXXXXXXX>"
```

If a `--space` argument was provided in `$ARGUMENTS`, use that space id.

## 5. Complete the OAuth login (inbound — optional)

```bash
cgc auth login
```

This runs the installed-app OAuth flow and caches the user token to the config
directory with restrictive permissions. Tokens are never logged.

## 6. Set the trigger prefix (optional)

The inbound listener recognizes commands whose message text starts with the
configured trigger prefix (default `claude-command:`). To override:

```bash
cgc config set trigger_prefix "claude-command:"
```

## 7. Verify

Send a test status message and confirm it succeeds:

```bash
cgc chat send --status info --text "claude-google-chat configured"
```

On success the command prints `sent` and exits `0`. On failure it exits non-zero
with the HTTP status code and the redacted webhook URL — surface that to the user
and do not retry silently.
