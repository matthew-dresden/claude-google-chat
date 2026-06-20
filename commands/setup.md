---
description: Run the guided cgc onboarding wizard (project, Chat API, auth, webhook, end-to-end verify) and diagnose prerequisites with cgc doctor.
argument-hint: ""
allowed-tools: Bash
disable-model-invocation: true
---

# Set up the Google Chat ChatOps integration

Run the guided `cgc` onboarding wizard to configure the integration end to end,
then verify prerequisites with `cgc doctor`. Configuration is written to the OS
user config directory by the CLI — **never to this repository or the working
directory**.

This is a one-time setup that must be completed before
`/claude-google-chat:connect`, `/claude-google-chat:send`, or
`/claude-google-chat:disconnect` will work.

Follow these steps in order. **Stop and report** the moment any step fails — do
not continue past a failure, and do not invent fallback behavior.

## 0. Verify the `cgc` CLI is installed

```bash
cgc --version
```

If the command is not found, `cgc` is missing from your `PATH`. Print these
install options and **stop** (do not attempt to proceed without `cgc`):

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

## 1. Run the guided wizard (`cgc setup`)

```bash
cgc setup
```

`cgc setup` is **idempotent and resumable** — each step is skipped when already
satisfied and re-running fixes only the gaps. It detects gcloud (or prints the
install link + console deep-links for manual setup), creates/selects a project,
enables the Chat API and polls until ready (active readiness, not `sleep`),
authenticates **ADC-first** with a guided OAuth-client fallback, validates the
incoming webhook (token never echoed), and **verifies a real send + read-back
round trip** before declaring success.

Useful variants:

```bash
cgc setup --reauth     # only redo authentication
cgc setup --dry-run    # show the actions that would run, change nothing
cgc setup --verify     # only run the end-to-end send/read round-trip check
```

On any failure it exits non-zero with a concise, actionable message (no traceback
or token) naming which step to re-run. Surface that to the user and **stop**.

## 2. Diagnose prerequisites (`cgc doctor`)

```bash
cgc doctor
```

`cgc doctor` prints a RED/GREEN (`[PASS]`/`[FAIL]`) checklist of every
prerequisite with the **exact fix command** per red line: gcloud installed /
logged in / project selected / Chat API enabled, OAuth-ADC credentials present &
valid, the required Chat scopes, `webhook_url` configured & well-formed (token
never echoed), `space_id` configured, and the config file. It **exits non-zero**
when any required check fails, so it doubles as a health gate.

If anything is red, surface the failing line(s) and the printed fix to the user
and **stop**.

## Next steps

Once setup is green, connect a session and start listening with
`/claude-google-chat:connect [name]`, and send status pings with
`/claude-google-chat:send <status> <text>`. See the
`/claude-google-chat:google-chat` skill for the session-bound protocol.
