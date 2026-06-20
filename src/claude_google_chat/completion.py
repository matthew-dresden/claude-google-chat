"""Shell-completion support for the ``cgc`` CLI.

This module is the single source of truth for two related concerns:

1. **Dynamic value completers** — Typer ``autocompletion`` callbacks that
   suggest live values (config keys, status labels, supported shells, and
   values derived from the current config such as ``space_id`` and
   ``trigger_prefix``). Every completer is wrapped so that any failure returns
   an empty list instead of propagating — a crashing completer must never break
   the user's shell.

2. **Completion-script generation/install** — thin wrappers around Typer's
   vendored Click completion machinery (the ``_CGC_COMPLETE`` protocol) used by
   the ``cgc completion <shell> [--install]`` command.

All value sets are derived from existing single sources of truth
(:data:`claude_google_chat.config.ENV_OVERRIDES` and
:data:`claude_google_chat.messages.ALLOWED_STATUSES`) so completion never drifts
from the real CLI behaviour and nothing is hard-coded twice.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterable
from pathlib import Path

# Typer (>=0.12) vendors Click; reach the completion-script helpers through the
# documented Typer entry points rather than importing ``click`` directly, so the
# code works whether Click is a standalone install or vendored under Typer.
from typer._completion_classes import completion_init
from typer._completion_shared import (
    _get_shell_name,
    get_completion_script,
)

from claude_google_chat.config import ENV_OVERRIDES, Config
from claude_google_chat.messages import ALLOWED_STATUSES

# Shells for which ``cgc completion`` can emit a script. Single source of truth
# shared by the ``--shell`` completer and the ``completion`` command validation.
SUPPORTED_COMPLETION_SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")


def _complete_var(prog_name: str) -> str:
    """Return the Click completion env var for ``prog_name`` (e.g. ``_CGC_COMPLETE``)."""
    return "_{}_COMPLETE".format(prog_name.replace("-", "_").upper())


def safe_completer(
    func: Callable[..., Iterable[str | tuple[str, str]]],
) -> Callable[..., list[str | tuple[str, str]]]:
    """Decorator making a completer crash-proof for the shell.

    A completion callback runs inside the user's interactive shell on every
    ``<TAB>``. If it raises, the shell sees a broken completion. This wrapper
    guarantees the callback always returns a list and never propagates an
    exception — on any error it returns ``[]`` (no suggestions) instead.
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> list[str | tuple[str, str]]:
        try:
            return list(func(*args, **kwargs))
        except Exception:
            # Never break the interactive shell on a completer failure.
            return []

    return wrapper


def _filter(values: Iterable[str], incomplete: str) -> list[str]:
    """Return the prefix-matching, de-duplicated, sorted ``values``."""
    seen: dict[str, None] = {}
    for value in values:
        if value and value.startswith(incomplete) and value not in seen:
            seen[value] = None
    return sorted(seen)


def _load_config_safely() -> Config | None:
    """Load the current config, returning ``None`` if it cannot be read.

    Completers must not fail when no config exists yet; a missing or malformed
    config simply yields no derived suggestions.
    """
    try:
        return Config.load()
    except Exception:
        return None


@safe_completer
def complete_config_key(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete known config keys for ``cgc config get|set <KEY>``.

    Keys and their environment-variable hints are derived from
    :data:`ENV_OVERRIDES` so the suggestions always match the keys the CLI
    actually understands.
    """
    items: list[tuple[str, str]] = [
        (key, f"env: {env_var}")
        for key, env_var in sorted(ENV_OVERRIDES.items())
        if key.startswith(incomplete)
    ]
    return list(items)


@safe_completer
def complete_status(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete ``--status`` values from :data:`ALLOWED_STATUSES`."""
    return list(_filter(ALLOWED_STATUSES, incomplete))


@safe_completer
def complete_shell(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete the ``--shell`` choices for ``cgc completion``."""
    return list(_filter(SUPPORTED_COMPLETION_SHELLS, incomplete))


@safe_completer
def complete_space_id(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete a ``space_id`` from the current config, if set."""
    config = _load_config_safely()
    if config is None or not config.space_id:
        return []
    return list(_filter([config.space_id], incomplete))


@safe_completer
def complete_trigger_prefix(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete a ``trigger_prefix`` from the current config, if set."""
    config = _load_config_safely()
    if config is None or not config.trigger_prefix:
        return []
    return list(_filter([config.trigger_prefix], incomplete))


@safe_completer
def complete_thread(incomplete: str) -> list[str | tuple[str, str]]:
    """Complete a thread resource name from the current config's ``threads``.

    Suggests the configured ``threads`` (``CGC_THREADS`` / config ``threads``)
    so ``cgc listen --thread`` can be completed from the user's known threads.
    Returns ``[]`` when none are configured.
    """
    config = _load_config_safely()
    if config is None or not config.threads:
        return []
    return list(_filter(config.threads, incomplete))


def render_completion_script(prog_name: str, shell: str) -> str:
    """Return the completion script for ``shell`` (wrapping Typer/Click).

    Raises ``ValueError`` (fail fast) when ``shell`` is not one of
    :data:`SUPPORTED_COMPLETION_SHELLS`, so callers can surface a clear,
    actionable message instead of an empty or partial script.
    """
    if shell not in SUPPORTED_COMPLETION_SHELLS:
        supported = ", ".join(SUPPORTED_COMPLETION_SHELLS)
        raise ValueError(f"unsupported shell {shell!r}; supported shells are: {supported}")
    # Register the bash/zsh/fish ``ShellComplete`` classes before generating.
    completion_init()
    return get_completion_script(
        prog_name=prog_name,
        complete_var=_complete_var(prog_name),
        shell=shell,
    )


def detect_shell() -> str | None:
    """Return the current shell name (lowercase) or ``None`` if undetectable."""
    return _get_shell_name()


# Map each supported shell to its interactive rc file (relative to ``$HOME``).
_RC_FILES: dict[str, str] = {
    "bash": ".bashrc",
    "zsh": ".zshrc",
    "fish": ".config/fish/config.fish",
}


def rc_path_for_shell(shell: str, home: Path | None = None) -> Path:
    """Return the rc file path for ``shell`` under ``home`` (defaults to ``$HOME``).

    Raises ``ValueError`` for an unsupported shell (fail fast).
    """
    rc_name = _RC_FILES.get(shell)
    if rc_name is None:
        supported = ", ".join(sorted(_RC_FILES))
        raise ValueError(f"unsupported shell {shell!r}; supported shells are: {supported}")
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base / rc_name


def install_completion_line(prog_name: str, shell: str, home: Path | None = None) -> Path:
    """Append an idempotent ``eval`` completion line to ``shell``'s rc file.

    The line evaluates the program's own completion *source* at shell start-up
    (``eval "$(env _CGC_COMPLETE=source_<shell> cgc)"``), so the installed
    completion always matches the installed CLI version — there is no static
    snapshot to drift. Re-running is a no-op when the exact line is already
    present. Returns the rc file path written.

    The instruction is ``source_<shell>`` (emit the completion-registration
    script), never ``complete_<shell>`` (perform a single completion). The
    ``complete_*`` instruction reads ``COMP_WORDS``/``_TYPER_COMPLETE_ARGS`` from
    the environment — values the shell only sets while a ``<TAB>`` is in flight.
    Putting ``complete_*`` in an rc file therefore runs the completion path at
    plain shell start-up with those variables absent, which raises and dumps a
    traceback into the user's terminal on every new shell. ``source_*`` has no
    such dependency and emits a clean registration script.

    Raises ``ValueError`` (fail fast) for an unsupported shell.
    """
    if shell not in SUPPORTED_COMPLETION_SHELLS:
        supported = ", ".join(SUPPORTED_COMPLETION_SHELLS)
        raise ValueError(f"unsupported shell {shell!r}; supported shells are: {supported}")

    rc_path = rc_path_for_shell(shell, home=home)
    complete_var = _complete_var(prog_name)
    if shell == "fish":
        # fish sources the generated script directly rather than via ``eval``.
        eval_line = f"env {complete_var}=source_fish {prog_name} | source"
    else:
        eval_line = f'eval "$(env {complete_var}=source_{shell} {prog_name})"'

    marker = f"# {prog_name} shell completion"
    block = f"{marker}\n{eval_line}\n"

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    existing = rc_path.read_text(encoding="utf-8") if rc_path.is_file() else ""
    if eval_line in existing:
        return rc_path

    prefix = "" if existing.endswith("\n") or existing == "" else "\n"
    rc_path.write_text(existing + prefix + block, encoding="utf-8")
    return rc_path
