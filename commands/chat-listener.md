---
description: Start the event-driven Google Chat listener and surface new commands prefixed with the configured trigger.
argument-hint: "[start|stop|status] [--once] [--timeout <seconds>]"
allowed-tools: Bash
disable-model-invocation: true
---

# Google Chat inbound listener

Control the event-driven Google Chat listener, which reads new messages from the
configured space and surfaces those prefixed with the configured trigger
(`claude-command:` by default).

Arguments passed to this command: `$ARGUMENTS`. The first token selects the
subcommand: `start` (default), `stop`, or `status`.

## start

Begin listening and emit each new triggered message as a structured JSON line to
stdout:

```bash
cgc listen $ARGUMENTS
```

- Each emitted line is a **structured JSON message** (the ChatOps envelope — see the
  `/claude-google-chat:google-chat` skill).
- `--once` drains the currently-pending messages and exits. Use this mode for hooks
  and CI, where a long-running process is undesirable.
- The listener is **poll/event driven** using an env-driven cadence
  (`CGC_POLL_INTERVAL`, default `2.0s`). It does **not** use `sleep` as a readiness
  primitive.
- The idle timeout is env-driven (`CGC_LISTEN_TIMEOUT`, default `0` = run forever).
  On timeout expiry the listener **fails fast** with a non-zero exit and a clear
  diagnostic — never a silent stop. A `--timeout <seconds>` flag overrides the env
  value for this run.

## status

Report whether a listener is currently running and the last-seen message marker:

```bash
cgc listen status
```

## stop

Signal a running listener to shut down gracefully:

```bash
cgc listen stop
```

If `cgc` reports the configuration is incomplete (for example a missing `space_id`
or `oauth_client_file`), instruct the user to run `/claude-google-chat:chat-setup`
first. Surface any non-zero exit and its diagnostic to the user; do not retry
silently.
