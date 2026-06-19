---
description: Send a status message to the configured Google Chat space via the incoming webhook.
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

Surface the HTTP result to the user. On a non-2xx response, report the status code
and the redacted webhook URL and **stop** — do not retry silently or fall back.

## Structured format

The message is sent as the structured ChatOps envelope: a human-readable summary
line (with a status emoji) followed by a fenced JSON envelope. For the full
protocol — envelope fields, status→emoji mapping, and how inbound commands are
recognized — see the `/claude-google-chat:google-chat` skill.

If `cgc` reports the configuration is incomplete (for example a missing
`webhook_url`), instruct the user to run `/claude-google-chat:chat-setup` first.
