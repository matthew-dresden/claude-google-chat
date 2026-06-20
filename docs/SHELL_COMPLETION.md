# Shell completion

`cgc` ships full tab completion for **bash** and **zsh** (and **fish**). Once enabled, pressing `<TAB>` completes commands, sub-groups, options, arguments, and a set of **dynamic values** derived live from the CLI and your current config — config keys, `--status` labels, supported shell names, file paths, and config-derived `space_id` / `trigger_prefix`.

This page is the complete reference for installing and customizing completion. For the rest of the command surface see [Usage](usage.md); for the config keys referenced below see [Configuration](configuration.md).

---

## Prerequisites

| Shell | Requirement |
|---|---|
| **bash** | The **`bash-completion`** package must be installed and sourced. Completion for `cgc` (like all Click/Typer-based tools) relies on the bash-completion framework being loaded in your shell. |
| **zsh** | `compinit` must be initialized (`autoload -U compinit && compinit`). This is on by default in most zsh setups, including Oh My Zsh. |
| **fish** | No extra package needed — fish loads completions natively. |

### Installing `bash-completion`

`bash-completion` is not always present by default. Install it with your platform package manager, then ensure your shell sources it.

```bash
# Debian / Ubuntu
sudo apt-get install bash-completion

# Fedora / RHEL
sudo dnf install bash-completion

# macOS (Homebrew) — installs bash-completion@2 for Bash 4+
brew install bash-completion@2
```

After installing, make sure your `~/.bashrc` (Linux) or `~/.bash_profile` (macOS) sources it. Most distro packages add this automatically; if not, add:

```bash
# Linux: usually already wired up by the package
[[ -f /usr/share/bash-completion/bash_completion ]] && . /usr/share/bash-completion/bash_completion

# macOS / Homebrew (path comes from `brew --prefix`)
[[ -r "$(brew --prefix)/etc/profile.d/bash_completion.sh" ]] && . "$(brew --prefix)/etc/profile.d/bash_completion.sh"
```

Open a new shell (or `source` the file) and confirm the framework is loaded:

```bash
type _init_completion >/dev/null 2>&1 && echo "bash-completion is loaded"
```

If that prints nothing, `bash-completion` is not yet active and `cgc` completion will not work until it is.

---

## Two ways to enable completion

There are two enabling strategies, and both are supported for **bash** and **zsh**:

1. **Auto-updating (recommended)** — evaluate the program's *live* completion source at shell start-up. There is no static snapshot, so completion always matches the installed `cgc` version. Nothing to regenerate after an upgrade.
2. **Static file install** — write the generated completion script to a file once. Faster shell start-up and works in locked-down environments, but you must regenerate the file after upgrading `cgc`.

Pick one per shell — do not enable both for the same shell.

> Under the hood `cgc completion <shell>` is a friendly wrapper over Typer's vendored Click completion machinery. Typer's native `cgc --install-completion` / `cgc --show-completion` flags also work and auto-detect the current shell; the `cgc completion` command additionally supports an explicit shell argument and an idempotent `--install`.

---

## bash

### Option A — auto-updating (recommended)

Append an `eval` line to your `~/.bashrc` so each new shell evaluates the current completion source:

```bash
cgc completion bash --install
```

This writes an **idempotent** block to `~/.bashrc`:

```bash
# cgc shell completion
eval "$(env _CGC_COMPLETE=source_bash cgc)"
```

The instruction is `source_bash` (emit the completion-registration script), **not** `complete_bash` (perform a single completion). `complete_bash` reads `COMP_WORDS` from the environment — a variable the shell only sets while a `<TAB>` is in flight — so putting it in `~/.bashrc` would run the completion path with `COMP_WORDS` unset at every shell start-up and print a traceback. `source_bash` has no such dependency.

Re-running `cgc completion bash --install` is a no-op when the exact line is already present, so it is safe in provisioning scripts. To wire it up by hand instead, add the same `eval` line yourself, or pipe the printed script:

```bash
echo 'eval "$(env _CGC_COMPLETE=source_bash cgc)"' >> ~/.bashrc
```

Open a new shell (or `source ~/.bashrc`) and completion is active. Because the `eval` runs the live source, **upgrading `cgc` needs no extra step**.

### Option B — static file install

Generate the script once and source it from a fixed location:

```bash
cgc completion bash > ~/.local/share/cgc/cgc-complete.bash
```

Then source it from `~/.bashrc`:

```bash
# cgc shell completion (static)
[[ -f ~/.local/share/cgc/cgc-complete.bash ]] && . ~/.local/share/cgc/cgc-complete.bash
```

Or drop it into the system completion directory so `bash-completion` loads it lazily (no `~/.bashrc` edit):

```bash
cgc completion bash > "$(pkg-config --variable=completionsdir bash-completion 2>/dev/null || echo /usr/share/bash-completion/completions)/cgc"
```

> **Trade-off:** a static file is a snapshot. After `pipx upgrade claude-google-chat` (or any version bump), **regenerate the file** so completion reflects new commands/options. The auto-updating option avoids this.

---

## zsh

### Option A — auto-updating (recommended)

```bash
cgc completion zsh --install
```

This appends an idempotent block to `~/.zshrc`:

```zsh
# cgc shell completion
eval "$(env _CGC_COMPLETE=source_zsh cgc)"
```

The instruction is `source_zsh` (emit the registration script), **not** `complete_zsh`; as with bash, the `complete_*` form reads completion state from the environment and would print a traceback at shell start-up.

Ensure `compinit` runs **before** that line (Oh My Zsh and most frameworks do this for you). A minimal manual setup:

```zsh
autoload -U compinit && compinit
eval "$(env _CGC_COMPLETE=source_zsh cgc)"
```

Open a new shell (or `source ~/.zshrc`). As with bash, the live `eval` means **no regeneration is needed after upgrades**.

### Option B — static file install

zsh loads completions from directories on its `$fpath`. Write the generated script as `_cgc` into a directory on `$fpath`:

```bash
mkdir -p ~/.zsh/completions
cgc completion zsh > ~/.zsh/completions/_cgc
```

Then, **before** `compinit` in `~/.zshrc`:

```zsh
fpath=(~/.zsh/completions $fpath)
autoload -U compinit && compinit
```

> **Trade-off:** like the bash static file, `_cgc` is a snapshot — **regenerate it after upgrading `cgc`**. You may also need to clear zsh's completion cache (`rm -f ~/.zcompdump*`) after replacing the file.

---

## What gets completed

Once enabled, tab completion suggests **commands, sub-groups, options, and arguments**, plus these dynamic values:

| Where | Completes |
|---|---|
| `cgc config get <key>` / `cgc config set <key>` | Known config keys, each with its `CGC_*` env-var hint. |
| `cgc chat send --status <TAB>` | `info`, `working`, `success`, `error`, `blocked`. |
| `cgc completion <shell>` / `--shell` | `bash`, `zsh`, `fish`. |
| `cgc auth login --client-file <TAB>` | File paths (native shell file completion). |
| `cgc serve --space-id` / `cgc listen --space-id` | The `space_id` from your current config, if set. |
| `cgc clear --trigger-prefix` | The `trigger_prefix` from your current config (defaults to `claude-command:`). |

The config-key and `--status` value sets are derived from the CLI's single sources of truth (`ENV_OVERRIDES` and `ALLOWED_STATUSES`), so suggestions can never drift from the commands `cgc` actually accepts.

**Dynamic completers never crash your shell:** every completer is wrapped so that any error (e.g. a missing or malformed config file) simply yields no suggestions instead of breaking your prompt.

---

## Unsupported shells fail fast

`cgc completion` supports exactly `bash`, `zsh`, and `fish`. Anything else exits non-zero with a clear, actionable message and prints nothing to source:

```bash
$ cgc completion powershell
unsupported shell 'powershell'; supported shells are: bash, zsh, fish
$ echo $?
2
```

If you omit the shell argument, `cgc` uses the detected shell. When the shell cannot be detected, it also fails fast (exit code 2) and asks you to pass one explicitly.

---

## Verifying completion works

After enabling and opening a new shell:

```bash
cgc <TAB><TAB>                 # lists top-level commands (config, auth, chat, listen, serve, clear, completion)
cgc chat send --status <TAB>   # lists: blocked  error  info  success  working
cgc config get <TAB>           # lists known config keys with env-var hints
```

If nothing happens for bash, confirm `bash-completion` is loaded (see [Prerequisites](#prerequisites)). For zsh, confirm `compinit` has run.

---

## Upgrading

- **Auto-updating install (Option A):** nothing to do. The next new shell evaluates the upgraded `cgc`'s completion source automatically.
- **Static file install (Option B):** regenerate the file after upgrading:

  ```bash
  cgc completion bash > ~/.local/share/cgc/cgc-complete.bash   # bash
  cgc completion zsh  > ~/.zsh/completions/_cgc                # zsh (then: rm -f ~/.zcompdump*; exec zsh)
  ```

---

## Next steps

- [Usage](usage.md) — full command reference and structured message examples.
- [Configuration](configuration.md) — config keys, precedence, and secret handling.
- [Installation](installation.md) — install the CLI and the Claude Code plugin.
