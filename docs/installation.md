# Installation

`claude-google-chat` ships two installable artifacts from a single codebase:

1. A **Claude Code plugin** (slash commands + a skill) installable from a marketplace.
2. A **Python CLI** (`cgc`) that the plugin commands shell out to.

Install **both**: the plugin gives you `/claude-google-chat:*` commands inside Claude Code, and the CLI does the actual work of talking to Google Chat.

---

## Prerequisites

- **Python 3.11 or newer** (the CLI uses the stdlib `tomllib` module).
- **[pipx](https://pipx.pypa.io/)** to install the CLI.
- **A Google Chat space** you can post to and (for inbound reading) a Google Cloud project.
- **Claude Code** (for the plugin half).

---

## 1. Install the CLI (Python)

### pipx (recommended)

```bash
pipx install claude-google-chat        # from PyPI once published
cgc --help
```

pipx keeps the CLI in its own isolated environment and puts `cgc` on your `PATH` — the recommended way to run a Python command-line tool.

### From source

```bash
git clone https://github.com/matthew-dresden/claude-google-chat
cd claude-google-chat
uv sync
uv run cgc --help
```

Verify:

```bash
cgc --version
```

> **Console-command name note:** the CLI installs a console command named `cgc`. An **unrelated** PyPI package is also named `cgc` and installs a command of the same name. The two are unlikely to be installed together, but if you already use that other `cgc` tool, whichever package is later on your `PATH` wins. The Python **distribution** name (`claude-google-chat`) does not collide; only the short command can. You can always invoke this tool unambiguously as `python -m claude_google_chat …` (or `uv run cgc …` from a source checkout).

---

## 2. Install the Claude Code plugin

Inside Claude Code:

```
/plugin marketplace add matthew-dresden/claude-google-chat
/plugin install claude-google-chat@claude-google-chat
```

The marketplace name and the plugin name are both `claude-google-chat`, so the install selector is `claude-google-chat@claude-google-chat`.

After installation you have these commands:

- `/claude-google-chat:setup` — guided onboarding (`cgc setup` + `cgc doctor`).
- `/claude-google-chat:connect` — connect this instance as a named session and listen for messages routed to it.
- `/claude-google-chat:disconnect` — remove a session from the registry.
- `/claude-google-chat:send` — send a structured status ping (optionally into a session thread).

And a skill:

- `/claude-google-chat:google-chat` — documents the session-bound ChatOps protocol so Claude can route, read, and produce structured messages.

The commands invoke the `cgc` CLI. If `cgc` is not on your `PATH`, `/claude-google-chat:setup` prints the install commands from step 1 and stops.

---

## 3. Onboarding: `cgc setup` (one command)

The fastest path is the guided wizard. It is **idempotent and resumable** — run it as many times as you like; each step is skipped when already satisfied and re-running fixes only what is still missing.

```bash
cgc setup
```

It walks every prerequisite, verifying each step before moving on:

1. **gcloud** — detects the `gcloud` CLI. If it is missing, it prints the install link plus console deep-links so you can create a project and enable the API by hand.
2. **Project** — creates a new Google Cloud project or selects an existing one.
3. **Chat API** — enables `chat.googleapis.com` and **polls until it reports ENABLED** (an active readiness check, never a fixed wait; the timeout is env-driven via `CGC_SETUP_ENABLE_TIMEOUT` and fails fast).
4. **Auth — ADC-first** — runs `gcloud auth application-default login` requesting the Chat + identity scopes (so there is **no OAuth client to create**), then probes whether the Chat API accepts that ADC token. If it does, ADC is used. If not, it falls back to a **guided OAuth-client** flow: it prints the exact console deep-links to create a Desktop OAuth client + consent screen, validates the downloaded client-secrets JSON shape, and runs `cgc auth login`. After **either** path it verifies the token carries every required Chat scope and re-auths if not (guarding silent scope-drop).
5. **Webhook** — prompts for the incoming-webhook URL, validates its shape, and stores it (the token is never echoed).
6. **Verify end-to-end** — sends a unique test message and reads it back before declaring success. On any failure it prints a concise, actionable error (never a raw traceback or token) naming which step to re-run.

Useful flags:

```bash
cgc setup --reauth     # only redo authentication
cgc setup --dry-run    # show the actions that would run, change nothing
cgc setup --verify     # only run the end-to-end send/read round-trip check
```

### Diagnose with `cgc doctor`

At any time, run the diagnostics checklist to see exactly what is and is not configured, with the precise fix for each red line:

```bash
cgc doctor
```

It prints a `[PASS]`/`[FAIL]` line for every prerequisite (gcloud installed / logged in / project selected / Chat API enabled / OAuth-ADC credentials present & valid / token scopes / webhook & space configured / config file present) and **exits non-zero** if any required check fails, so you can use it as a health gate.

> **One path, always two-way.** There is no longer a separate "send-only" tier: the tool is the single user-OAuth/ADC two-way integration. Outbound status pings still go through the **incoming webhook**, and inbound reading uses the Chat REST API with your **ADC or OAuth user** credentials — `cgc setup` configures both.

---

## 4. Manual Google Cloud setup (alternative to `cgc setup`)

If you prefer to configure things by hand, you need an **incoming webhook** (for outbound sends) and an **ADC or OAuth client** (for inbound reading).

### 4a. Create an incoming webhook (outbound)

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

### 4b. Authenticate for inbound read/listen (ADC-first, OAuth-client fallback)

Reading inbound commands uses the Google Chat REST API. There are two ways to authenticate; `cgc setup` tries them in this order, and you can do either by hand:

**ADC (recommended, no OAuth client to create):**

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/chat.messages,openid,https://www.googleapis.com/auth/userinfo.email
cgc config set space_id "spaces/AAAA"
```

If the Chat API accepts the resulting Application Default Credentials, you are done — there is no OAuth client to register.

**OAuth client (fallback, if ADC is not accepted):**

1. In the [Google Cloud Console](https://console.cloud.google.com/), select or create a project.
2. Enable the **Google Chat API** for the project.
3. Configure the **OAuth consent screen** (internal is fine for a single Workspace).
4. Create an **OAuth client ID** of type **Desktop app**. Download the client secrets JSON.
5. Point the CLI at it and complete the installed-app flow once (the user token is cached locally with restrictive permissions):

   ```bash
   cgc config set oauth_client_file "/path/to/oauth_client.json"
   cgc config set space_id "spaces/AAAA"
   cgc auth login
   ```

Either way, the required Chat scope is `https://www.googleapis.com/auth/chat.messages`. Outbound sends still use the webhook; ADC/OAuth is only required for read/listen/delete. (`cgc setup` validates, after either path, that the token actually carries the required scope and re-auths if not.)

---

## 5. Verify

```bash
cgc doctor                                       # RED/GREEN prerequisite checklist
cgc setup --verify                               # real send + read-back round trip
cgc config show                                  # masks secrets
cgc chat send --status info --text "hello from claude-google-chat"
cgc listen --once                                # drains pending messages and exits
```

`cgc doctor` exits non-zero if any required prerequisite is missing (and names the exact fix). A successful send prints `sent` and exits `0`; a failed send exits non-zero with the HTTP status code and a redacted URL. If anything required is missing, the CLI fails fast with a clear message naming the missing key — it never silently falls back to a default for a secret.

---

## 6. Releases and PyPI publishing (maintainers)

The project ships from a single codebase via three GitHub Actions workflows (uv + hatchling):

- **`.github/workflows/ci.yml`** — runs on every pull request and push to `main`: install, lint, format check, typecheck, version-consistency check (`pyproject.toml` vs `src/claude_google_chat/__init__.py`), manifest validation, tests, and a `uv build`. This is the merge-validation gate; it never touches PyPI.
- **`.github/workflows/release.yml`** — runs on push to `main`. It re-validates, reads the version from `pyproject.toml` (the single source of truth), and if the tag `v<version>` does **not** already exist it builds + `twine check`s the artifacts, cuts an **annotated** git tag `v<version>`, and creates a **GitHub Release** carrying the built `dist/*`. It is **idempotent**: if the tag already exists it is a clean no-op, so re-running on `main` without a version bump cuts nothing. (Version `0.1.0` / tag `v0.1.0` already exists.)
- **`.github/workflows/publish.yml`** — publishes to PyPI with `pypa/gh-action-pypi-publish`. It triggers **only** on a published GitHub Release (or manual `workflow_dispatch` with a `tag` input), so it is **never part of merge CI** — an unconfigured PyPI setup can never block or break pull-request / push-to-main validation. It runs in a GitHub Environment named **`pypi`**.

### Cutting a new release

1. Bump the version in **both** `pyproject.toml` (`[project].version`) and `src/claude_google_chat/__init__.py` (`__version__`). They must match — CI fails otherwise.
2. Move the relevant `## [Unreleased]` entries in [CHANGELOG.md](../CHANGELOG.md) into a new `## [x.y.z]` section.
3. Merge to `main`. `release.yml` tags `v<x.y.z>` and creates the GitHub Release, which in turn triggers `publish.yml`.

### Finishing PyPI setup (required once, before automated publish works)

Automated publishing stays inert until you complete **exactly one** of these. Until then, releases still succeed; only the publish step at the end of a release would fail.

**Option A (recommended) — OIDC Trusted Publishing, no stored secret:**

1. On [PyPI](https://pypi.org), own/register the project `claude-google-chat` (or run `make publish` once manually — see below — to create it).
2. Project → **Settings → Publishing → Add a pending publisher** with:
   - Publisher: **GitHub**
   - Owner: **matthew-dresden**
   - Repository: **claude-google-chat**
   - Workflow filename: **publish.yml**
   - Environment name: **pypi**
3. In GitHub: **Settings → Environments → create `pypi`** (optionally add required reviewers as a human gate). Leave the publish step's `with:` without a `password:` — OIDC authenticates automatically.

**Option B — API token (fallback):**

1. Create a PyPI API token scoped to `claude-google-chat`.
2. GitHub: **Settings → Environments → `pypi` → add secret `PYPI_API_TOKEN`**.
3. Uncomment the `password: ${{ secrets.PYPI_API_TOKEN }}` line in `publish.yml`.

### Manual publish (validate before relying on automation)

To validate a publish by hand (e.g. to create the project on PyPI the first time), use the `publish` Makefile target. It builds, `twine check`s, then `uv publish`, reading the token from the environment — never hardcoded:

```bash
export UV_PUBLISH_TOKEN=pypi-...   # a PyPI API token
make publish
```

---

## Next steps

- [Configuration](configuration.md) — full config reference, precedence, secret handling.
- [Usage](usage.md) — command reference and structured message examples.
- [Architecture](architecture.md) — how the pieces fit together.
