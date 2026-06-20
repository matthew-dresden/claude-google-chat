---
description: Send a status message to the Google Chat space via the incoming webhook (optionally into a session thread with --thread-key).
argument-hint: "<status> <text>"
allowed-tools: Bash
disable-model-invocation: true
---

# Send a Google Chat status ping

Send a structured status message to the configured Google Chat space using the
incoming webhook. The first token of the arguments is the **status** and the
remainder is the **text**.

- Status (first token): `$0` — one of `info | working | success | error | blocked`.
- Full argument string: `$ARGUMENTS`.

Extract the leading status token, treat the rest of `$ARGUMENTS` as the message
text, then run:

```bash
cgc chat send --status "$0" --text "<remaining text after the status token>"
```

On success the command prints `sent` and exits `0`. On a non-2xx response it exits
non-zero with the status code and the redacted webhook URL — report that and
**stop**; do not retry silently or fall back.

## Replying within a session thread (`--thread-key`)

When replying to a message that arrived on a session thread (e.g. from
`cgc listen --session <name>`), post the reply **into that same thread** by
passing the event's `thread_name` as `--thread-key`:

```bash
cgc chat send --status success --text "<reply>" --thread-key "<thread_name>"
```

Repeated sends with the same key land in the same thread; an unseen key starts a
new one. Without `--thread-key`, the message is posted unthreaded to the space.

## Clean by default

The message posted to Chat is the clean, emoji-prefixed summary line alone — the
machine-readable JSON envelope is **not** embedded in the human-facing Chat view
unless you opt in with `--envelope`. For the full session-bound protocol —
envelope fields, status→emoji mapping, and thread routing — see the
`/claude-google-chat:google-chat` skill.

If `cgc` is missing from your `PATH`, or it reports the configuration is
incomplete (for example a missing `webhook_url`), tell the user to run
`/claude-google-chat:setup` first.
