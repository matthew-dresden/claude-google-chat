"""Functional + integration + human-scenario tests for ``cgc`` shell completion.

These tests do **not** call the completer callbacks in-process (that is what
:mod:`tests.test_completion` does). Instead they reproduce *exactly* what a real
shell does when a user presses ``<TAB>``: they spawn the ``cgc`` CLI as a
subprocess with the Click/Typer completion environment variables set, and they
source the generated completion script in a real ``bash`` (and ``zsh`` when
available) and drive the registered completion function.

The single, load-bearing invariant under test is:

    **When a user presses ``<TAB>``, NOTHING weird may appear** — no Python
    traceback, no stderr text, no log lines, no stray stdout, no error message;
    only the actual completions.

Every scenario therefore asserts three things together:

1. the **correct** completions are produced (real behaviour, not "it ran"),
2. **stderr is empty**, and
3. the combined output contains **no diagnostic artifact** — no ``Traceback``,
   no ``Error:``/``Warning:`` diagnostic line, no Rich traceback box. Legitimate
   completion *values* that merely contain a substring like ``error`` (the
   ``--status error`` choice) are explicitly allowed; only diagnostic *patterns*
   fail the test.

Why a subprocess and a real shell rather than ``CliRunner``: the artifact class
we are guarding against (a completer or the completion machinery writing to
stdout/stderr, or the rc ``eval`` line invoking the wrong completion
instruction) only manifests through the real process/shell boundary. The genuine
``cgc`` console script (discovered next to the test interpreter) is invoked so
the completion protocol's basename-derived ``_CGC_COMPLETE`` env var resolves
exactly as it does for a real user; the config dir is redirected to keep it
hermetic.

The config directory is redirected to a per-test ``tmp_path`` via
``XDG_CONFIG_HOME`` so no real OS config dir is read or written, and every
scenario is also run with **no config file**, a **broken/partial config**, and
with the ``CGC_*`` environment overrides both set and unset — the historical
sources of completion artifacts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_google_chat.config import ENV_OVERRIDES
from claude_google_chat.messages import ALLOWED_STATUSES

# --------------------------------------------------------------------------- #
# Invocation plumbing.
# --------------------------------------------------------------------------- #

PROG_NAME = "cgc"
COMPLETE_VAR = "_CGC_COMPLETE"


def _resolve_console_script(
    interpreter: str,
    prog_name: str,
    which: Callable[[str], str | None],
) -> str:
    """Resolve the path to ``prog_name`` next to ``interpreter`` or via ``which``.

    Pure resolution logic with the filesystem/``PATH`` lookup injected, so every
    branch — including the "not found anywhere" failure — is exercised by a unit
    test (no coverage suppression needed). Raises ``RuntimeError`` (fail fast)
    when the console script cannot be located.
    """
    candidate = Path(interpreter).with_name(prog_name)
    if candidate.exists():
        return str(candidate)
    found = which(prog_name)
    if found is None:
        raise RuntimeError(
            f"{prog_name!r} console script not found next to {interpreter!r} or on PATH"
        )
    return found


def _discover_cgc() -> str:
    """Locate the real ``cgc`` console script (input-driven, never hard-coded).

    The completion protocol keys off the program's *basename* (``cgc`` ->
    ``_CGC_COMPLETE``), so the test must invoke the genuine console script, not
    ``python -m`` (whose argv[0] would not be ``cgc``). The script sits next to
    the interpreter running the tests; fall back to ``PATH`` lookup.
    """
    return _resolve_console_script(sys.executable, PROG_NAME, shutil.which)


# The genuine console script users invoke (so the completion protocol's
# basename-derived env var resolves correctly).
CGC_BIN: str = _discover_cgc()
CGC_ARGV: tuple[str, ...] = (CGC_BIN,)

# Diagnostic patterns that must NEVER appear in completion output. These match
# real leak shapes (tracebacks, log levels, Rich error boxes, "Error:"/"Warning:"
# prefixes) while deliberately NOT matching a bare completion value such as the
# ``error`` status choice or a ``space_id`` containing letters.
_ARTIFACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Traceback", re.IGNORECASE),
    re.compile(r"\bError:"),
    re.compile(r"\bWarning:"),
    re.compile(r"\bException\b"),
    re.compile(r"^\s*File \".*\", line \d+", re.MULTILINE),
    re.compile(r"\b(?:CRITICAL|ERROR|WARNING):"),  # logging.basicConfig format
    re.compile(r"not supported", re.IGNORECASE),  # Typer "Shell ... not supported."
    re.compile(r"Invalid completion"),
    re.compile(r"[╭╰│❱]"),  # Rich traceback box-drawing characters
)

# Timeout for any single completion subprocess (configurable, never hard-coded
# in the assertion path); a completion that hangs is itself a failure.
_TIMEOUT_S = float(os.environ.get("CGC_COMPLETION_TEST_TIMEOUT", "30"))


@dataclass(frozen=True)
class CompletionResult:
    """The captured outcome of one completion invocation."""

    stdout: str
    stderr: str
    returncode: int

    @property
    def values(self) -> list[str]:
        """Completion candidate tokens parsed from stdout, across shells.

        Typer's bash output is one bare ``value`` per line (optionally a TAB and
        help text). Typer's zsh output is an ``_arguments '*: :(( ... ))'`` block
        whose candidates are quoted as either ``"value":"help"`` (when a help
        string exists) or a bare ``"value"`` (when it does not), and several may
        share a line. Both are reduced to the bare candidate tokens so one
        assertion style works regardless of which shell drove the completion.
        """
        text = self.stdout
        # zsh shape: extract the candidate tokens from inside the (( ... )) block.
        if "_arguments" in text:
            tokens: list[str] = []
            # A candidate ``"value"`` is a quoted string that begins right after a
            # candidate boundary -- an opening paren or any whitespace/newline --
            # whereas a ``"help"`` description always begins right after a ``:``.
            # Anchoring on the boundary distinguishes values from descriptions
            # even when a description wraps across lines.
            for m in re.finditer(r'[(\s]"((?:[^"\\]|\\.)*)"', text):
                token = m.group(1)
                # Drop the ``_arguments`` template fragments, never candidates.
                if token in {"", "*", "*: :"} or token.startswith("*: "):
                    continue
                # zsh escapes ``:`` as ``\:`` (sometimes doubly, ``\\:``) inside a
                # candidate; collapse any run of backslashes before a colon.
                tokens.append(re.sub(r"\\+:", ":", token))
            return tokens
        # bash shape: one bare token per line.
        out: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                out.append(line.split("\t", 1)[0])
        return out


def _assert_clean(result: CompletionResult, *, context: str) -> None:
    """Assert the completion produced no artifact and exited cleanly.

    This is the heart of the suite: stderr must be empty, the exit code zero,
    and neither stream may contain a diagnostic pattern. ``context`` names the
    human scenario so a failure points straight at the offending ``<TAB>``.
    """
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.stderr == "", (
        f"[{context}] completion wrote to STDERR (a shell artifact):\n{result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"[{context}] completion exited {result.returncode} (expected 0); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    for pattern in _ARTIFACT_PATTERNS:
        match = pattern.search(combined)
        assert match is None, (
            f"[{context}] completion output contains artifact "
            f"{pattern.pattern!r} -> {match.group(0)!r}\nfull output:\n{combined}"
        )


def _run_complete_bash(comp_line: str, *, env: Mapping[str, str]) -> CompletionResult:
    """Drive Typer's bash completion protocol for ``comp_line``.

    Reproduces the generated bash function, which calls the CLI with
    ``COMP_WORDS`` (space-joined words) and ``COMP_CWORD`` (cursor word index)
    in the environment and ``_CGC_COMPLETE=complete_bash``.
    """
    words = comp_line.split(" ")
    # A trailing space means the cursor is on a fresh (empty) word.
    if comp_line.endswith(" "):
        words = words[:-1] + [""]
    cword = len(words) - 1
    proc_env = dict(env)
    proc_env[COMPLETE_VAR] = "complete_bash"
    proc_env["COMP_WORDS"] = " ".join(words)
    proc_env["COMP_CWORD"] = str(cword)
    completed = subprocess.run(
        CGC_ARGV,
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    return CompletionResult(completed.stdout, completed.stderr, completed.returncode)


def _run_complete_zsh(comp_line: str, *, env: Mapping[str, str]) -> CompletionResult:
    """Drive Typer's zsh completion protocol for ``comp_line``.

    The generated zsh function passes the words up to the cursor through
    ``_TYPER_COMPLETE_ARGS`` and requests ``_CGC_COMPLETE=complete_zsh``.
    """
    proc_env = dict(env)
    proc_env[COMPLETE_VAR] = "complete_zsh"
    proc_env["_TYPER_COMPLETE_ARGS"] = comp_line
    completed = subprocess.run(
        CGC_ARGV,
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    return CompletionResult(completed.stdout, completed.stderr, completed.returncode)


# --------------------------------------------------------------------------- #
# Environment / config fixtures (no real OS config dir is ever touched).
# --------------------------------------------------------------------------- #


def _base_env(config_home: Path) -> dict[str, str]:
    """A minimal, hermetic environment pointing config I/O at ``config_home``.

    Strips every ``CGC_*`` override so the "env unset" scenarios are
    deterministic; individual tests re-add overrides as needed.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(config_home),
        "XDG_CONFIG_HOME": str(config_home),
        # Keep the interpreter able to import the package from a subprocess.
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    return env


@pytest.fixture
def config_home(tmp_path: Path) -> Path:
    """A per-test config home (``XDG_CONFIG_HOME``) with no config file yet."""
    home = tmp_path / "xdg"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def env_no_config(config_home: Path) -> dict[str, str]:
    """Environment with a config home but no ``config.toml`` written."""
    return _base_env(config_home)


@pytest.fixture
def env_with_config(config_home: Path) -> dict[str, str]:
    """Environment whose config home holds a valid, populated ``config.toml``."""
    cfg_dir = config_home / "claude-google-chat"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        'space_id = "spaces/FUNCTIONAL"\n'
        'trigger_prefix = "ops-command:"\n'
        'webhook_url = "https://hook/func"\n',
        encoding="utf-8",
    )
    return _base_env(config_home)


@pytest.fixture
def env_broken_config(config_home: Path) -> dict[str, str]:
    """Environment whose ``config.toml`` is malformed/partial TOML.

    A broken config historically made config-derived completers raise; the
    completer must instead yield nothing, cleanly.
    """
    cfg_dir = config_home / "claude-google-chat"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        'this is = not valid toml [[[\npoll_interval = "not-a-number"\n',
        encoding="utf-8",
    )
    return _base_env(config_home)


# --------------------------------------------------------------------------- #
# Shell parametrization: bash always, zsh only when the binary exists.
# --------------------------------------------------------------------------- #

_ZSH = shutil.which("zsh")
_BASH = shutil.which("bash")

_DRIVERS = {"bash": _run_complete_bash, "zsh": _run_complete_zsh}

_SHELL_PARAMS = [
    pytest.param("bash", id="bash"),
    pytest.param(
        "zsh",
        id="zsh",
        marks=pytest.mark.skipif(_ZSH is None, reason="zsh not installed; bash still covered"),
    ),
]


def _drive(shell: str, comp_line: str, env: Mapping[str, str]) -> CompletionResult:
    return _DRIVERS[shell](comp_line, env=env)


# --------------------------------------------------------------------------- #
# Human-like scenarios: correct completions AND clean output, per shell.
# --------------------------------------------------------------------------- #

# Top-level commands a fresh ``cgc <TAB>`` must offer (subset asserted so the
# test does not over-specify ordering or future additions).
_EXPECTED_TOP_LEVEL = {"config", "auth", "chat", "listen", "clear", "completion"}


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_top_level_commands(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc <TAB>`` lists the top-level commands, cleanly."""
    result = _drive(shell, "cgc ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc <TAB>")
    assert _EXPECTED_TOP_LEVEL.issubset(set(result.values)), result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_partial_co_completes_config_and_completion(
    shell: str, env_no_config: dict[str, str]
) -> None:
    """``cgc co<TAB>`` narrows to ``completion`` and ``config`` only."""
    result = _drive(shell, "cgc co", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc co<TAB>")
    assert set(result.values) == {"completion", "config"}, result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_config_set_completes_keys(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc config set <TAB>`` offers every known config key."""
    result = _drive(shell, "cgc config set ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc config set <TAB>")
    assert set(ENV_OVERRIDES).issubset(set(result.values)), result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_config_get_completes_keys(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc config get <TAB>`` offers every known config key."""
    result = _drive(shell, "cgc config get ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc config get <TAB>")
    assert set(ENV_OVERRIDES).issubset(set(result.values)), result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_chat_send_status_completes_statuses(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc chat send --status <TAB>`` offers exactly the allowed statuses.

    Note the ``error`` status value is a legitimate candidate: the artifact
    check must NOT flag it, which is why ``_ARTIFACT_PATTERNS`` matches only
    diagnostic shapes (``Error:``), never a bare ``error`` token.
    """
    result = _drive(shell, "cgc chat send --status ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc chat send --status <TAB>")
    assert set(ALLOWED_STATUSES).issubset(set(result.values)), result.values
    assert "error" in result.values  # legitimate value, must survive the artifact filter


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_completion_shell_argument(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc completion <TAB>`` offers bash/zsh/fish."""
    result = _drive(shell, "cgc completion ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc completion <TAB>")
    assert {"bash", "zsh", "fish"}.issubset(set(result.values)), result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_auth_login_client_file_does_not_crash(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc auth login --client-file <TAB>`` (file path) must not crash.

    File completion is delegated to the shell's default mechanism; the CLI must
    emit nothing diagnostic regardless of what (if anything) it returns.
    """
    result = _drive(shell, "cgc auth login --client-file ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc auth login --client-file <TAB>")


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_space_id_completion_clean_without_config(
    shell: str, env_no_config: dict[str, str]
) -> None:
    """``cgc listen --space-id <TAB>`` is clean with NO config file.

    This is the canonical artifact source: a config-derived completer firing
    when no config exists. It must produce no suggestions and no diagnostics.
    """
    result = _drive(shell, "cgc listen --space-id ", env_no_config)
    _assert_clean(result, context=f"{shell}: cgc listen --space-id <TAB> (no config)")


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_space_id_completion_offers_configured_value(
    shell: str, env_with_config: dict[str, str]
) -> None:
    """With a config file, ``--space-id <TAB>`` offers the configured space id."""
    result = _drive(shell, "cgc listen --space-id ", env_with_config)
    _assert_clean(result, context=f"{shell}: cgc listen --space-id <TAB> (with config)")
    assert "spaces/FUNCTIONAL" in result.values, result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_trigger_prefix_completion_clean(shell: str, env_with_config: dict[str, str]) -> None:
    """``cgc clear --trigger-prefix <TAB>`` completes the configured prefix."""
    result = _drive(shell, "cgc clear --trigger-prefix ", env_with_config)
    _assert_clean(result, context=f"{shell}: cgc clear --trigger-prefix <TAB>")
    assert "ops-command:" in result.values, result.values


# --------------------------------------------------------------------------- #
# Robustness matrix: broken config, and env overrides set/unset.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
@pytest.mark.parametrize(
    "comp_line",
    [
        "cgc ",
        "cgc listen --space-id ",
        "cgc clear --trigger-prefix ",
        "cgc config set ",
    ],
)
def test_completion_clean_with_broken_config(
    shell: str, comp_line: str, env_broken_config: dict[str, str]
) -> None:
    """No completer leaks a traceback when ``config.toml`` is malformed."""
    result = _drive(shell, comp_line, env_broken_config)
    _assert_clean(result, context=f"{shell}: {comp_line!r} (broken config)")


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_space_id_completion_with_env_override_set(
    shell: str, env_no_config: dict[str, str]
) -> None:
    """A ``CGC_SPACE_ID`` env override is surfaced by ``--space-id <TAB>``.

    Exercises the "env vars set" branch with NO config file present, which must
    still be clean and reflect the override.
    """
    env = dict(env_no_config)
    env[ENV_OVERRIDES["space_id"]] = "spaces/FROM_ENV"
    result = _drive(shell, "cgc listen --space-id ", env)
    _assert_clean(result, context=f"{shell}: --space-id <TAB> (env override set)")
    assert "spaces/FROM_ENV" in result.values, result.values


@pytest.mark.parametrize("shell", _SHELL_PARAMS)
def test_completion_clean_with_all_env_overrides_unset(
    shell: str, env_no_config: dict[str, str]
) -> None:
    """Every ``CGC_*`` override explicitly unset: still clean (env-unset branch)."""
    env = dict(env_no_config)
    for env_var in ENV_OVERRIDES.values():
        env.pop(env_var, None)
    result = _drive(shell, "cgc listen --space-id ", env)
    _assert_clean(result, context=f"{shell}: --space-id <TAB> (all env unset)")


# --------------------------------------------------------------------------- #
# Completion-script generation + real-shell sourcing.
# --------------------------------------------------------------------------- #


def _run_cgc(args: list[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CGC_ARGV, *args],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_script_is_nonempty_and_clean(shell: str, env_no_config: dict[str, str]) -> None:
    """``cgc completion <shell>`` prints a real script to stdout with no stderr."""
    proc = _run_cgc(["completion", shell], env_no_config)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr
    assert proc.stdout.strip() != ""
    assert COMPLETE_VAR in proc.stdout
    assert PROG_NAME in proc.stdout


@pytest.mark.skipif(_BASH is None, reason="bash not installed")
def test_bash_completion_script_sources_and_drives_cleanly(
    env_with_config: dict[str, str],
) -> None:
    """Source the generated bash script in a real bash and drive a ``<TAB>``.

    This is the closest test to a human at a prompt: the completion script is
    sourced (defining ``_cgc_completion`` and the ``complete`` registration),
    then the function is invoked with ``COMP_WORDS``/``COMP_CWORD`` exactly as
    bash sets them on ``<TAB>``. The whole interaction must be artifact-free and
    must yield the expected candidates.
    """
    script = _run_cgc(["completion", "bash"], env_with_config)
    assert script.returncode == 0 and script.stderr == "", script.stderr

    # The function re-invokes ``cgc``; route that back to ``python -m`` via a
    # tiny shim on PATH so the sourced script finds the same CLI hermetically.
    shim_dir = Path(env_with_config["HOME"]) / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "cgc"
    shim.write_text(
        f'#!/usr/bin/env bash\nexec {CGC_BIN} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    driver = (
        "source /dev/stdin <<'CGC_EOF'\n"
        f"{script.stdout}\n"
        "CGC_EOF\n"
        "COMP_WORDS=(cgc co)\n"
        "COMP_CWORD=1\n"
        "_cgc_completion cgc\n"
        'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    env = dict(env_with_config)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    completed = subprocess.run(
        [_BASH, "--noprofile", "--norc", "-c", driver],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    assert completed.stderr == "", f"sourcing/driving bash leaked stderr:\n{completed.stderr}"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    candidates = {ln.strip() for ln in completed.stdout.splitlines() if ln.strip()}
    assert {"completion", "config"} <= candidates, completed.stdout


@pytest.mark.skipif(_BASH is None, reason="bash not installed")
def test_installed_rc_eval_line_sources_without_error(
    env_no_config: dict[str, str], tmp_path: Path
) -> None:
    """The rc ``eval`` line written by ``--install`` must source cleanly.

    Regression guard for the artifact bug where the installed line used the
    ``complete_<shell>`` instruction (which reads ``COMP_WORDS`` from the env,
    absent at shell start-up) and dumped a traceback in every new shell. The
    correct ``source_<shell>`` instruction emits a registration script that
    sources with no stderr and no traceback.
    """
    from claude_google_chat.completion import install_completion_line

    rc_path = install_completion_line(PROG_NAME, "bash", home=tmp_path)
    eval_line = rc_path.read_text(encoding="utf-8")
    # Must request the source instruction, never the complete instruction.
    assert "source_bash" in eval_line
    assert "complete_bash" not in eval_line

    # A shim so the eval line's ``cgc`` resolves to this interpreter.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "cgc"
    shim.write_text(
        f'#!/usr/bin/env bash\nexec {CGC_BIN} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    env = dict(env_no_config)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    # Sourcing the rc file at "shell start-up" must not write anything.
    completed = subprocess.run(
        [_BASH, "--noprofile", "--norc", "-c", f"source {rc_path}; echo READY"],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    assert completed.stderr == "", f"rc eval line leaked at start-up:\n{completed.stderr}"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Traceback" not in completed.stdout
    assert "READY" in completed.stdout


@pytest.mark.skipif(_ZSH is None, reason="zsh not installed")
def test_zsh_completion_script_sources_under_compinit(
    env_no_config: dict[str, str],
) -> None:
    """The generated zsh script sources cleanly once ``compinit`` is initialized.

    zsh completion scripts use ``compdef``, which only exists after
    ``compinit``; the test initializes it (as a real zsh user's setup does) and
    asserts the ``cgc`` script registers without writing to stderr.
    """
    script = _run_cgc(["completion", "zsh"], env_no_config)
    assert script.returncode == 0 and script.stderr == "", script.stderr

    driver = (
        "autoload -Uz compinit && compinit -u\n"
        "source /dev/stdin <<'CGC_EOF'\n"
        f"{script.stdout}\n"
        "CGC_EOF\n"
        "print -- READY\n"
    )
    completed = subprocess.run(
        [_ZSH, "-f", "-c", driver],
        env=dict(env_no_config),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    assert "Traceback" not in (completed.stdout + completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "READY" in completed.stdout


# --------------------------------------------------------------------------- #
# _resolve_console_script: pure resolution logic, all branches covered.
# --------------------------------------------------------------------------- #


def test_resolve_console_script_prefers_sibling(tmp_path: Path) -> None:
    """A console script next to the interpreter is returned without a PATH lookup."""
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    sibling = tmp_path / PROG_NAME
    sibling.write_text("", encoding="utf-8")

    def _never(_: str) -> str | None:
        raise AssertionError("which must not be consulted when a sibling exists")

    assert _resolve_console_script(str(interpreter), PROG_NAME, _never) == str(sibling)


def test_resolve_console_script_falls_back_to_path(tmp_path: Path) -> None:
    """With no sibling, the PATH lookup result is returned."""
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")

    assert (
        _resolve_console_script(str(interpreter), PROG_NAME, lambda _: "/usr/bin/cgc")
        == "/usr/bin/cgc"
    )


def test_resolve_console_script_raises_when_absent(tmp_path: Path) -> None:
    """When neither a sibling nor PATH has the script, it fails fast."""
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        _resolve_console_script(str(interpreter), PROG_NAME, lambda _: None)
    assert PROG_NAME in str(exc_info.value)
