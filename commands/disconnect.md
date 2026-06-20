---
description: Disconnect a Google Chat session, removing it from the registry (and re-electing a dispatcher if needed).
argument-hint: "[name]"
allowed-tools: Bash
disable-model-invocation: true
---

# Disconnect a Google Chat session

Remove a named session from the local registry so messages are no longer routed
to it. If the removed session was the dispatcher and other sessions remain, the
CLI promotes a survivor to dispatcher automatically.

Session NAME passed to this command: `$ARGUMENTS`.

Follow these steps in order. **Stop and report** the moment a step fails.

## 0. Verify the `cgc` CLI is installed

```bash
cgc --version
```

If the command is not found, tell the user to install `cgc` and run
`/claude-google-chat:setup`, then **stop**.

## 1. Resolve the session NAME

If `$ARGUMENTS` is empty, list the registered sessions so the user can pick one
(and to confirm the exact name):

```bash
cgc session list
```

`cgc disconnect` requires an explicit NAME — there is no auto-derivation on
disconnect. Ask the user which session to disconnect if it is ambiguous.

## 2. Disconnect

```bash
cgc disconnect $ARGUMENTS
```

- Removes the named session from the registry; if it was the dispatcher and
  others remain, a survivor is promoted.
- Add `--notify` to post a "session disconnected" note to its primary thread
  before removal.
- An unknown NAME fails fast with a non-zero exit — surface that and **stop**.

On success it prints `disconnected <name>`. If a routing-aware listener
(`cgc listen --session <name>`) is still running for that session, stop it
(Ctrl-C) — it will fail fast on its next poll since the session no longer exists.
