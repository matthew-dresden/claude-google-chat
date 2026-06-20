"""Tests for shell-completion support (:mod:`claude_google_chat.completion`).

These exercise the real completer callbacks, the crash-proof wrapper, and the
script/rc-file install helpers. Every completer is asserted on concrete values
derived from the single sources of truth (``ENV_OVERRIDES``, ``ALLOWED_STATUSES``)
so the suggestions cannot drift from the CLI's real behaviour. All config and
rc-file I/O is redirected to ``tmp_path`` so no real OS config dir or shell rc
file is touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_google_chat import completion
from claude_google_chat.config import ENV_OVERRIDES
from claude_google_chat.messages import ALLOWED_STATUSES


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip CGC_* env overrides so config-derived completers are deterministic."""
    for env_var in ENV_OVERRIDES.values():
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture
def patched_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``default_config_path`` so ``Config.load`` reads a temp file."""
    path = tmp_path / "cgc" / "config.toml"

    def _fake_path() -> Path:
        return path

    monkeypatch.setattr("claude_google_chat.config.default_config_path", _fake_path)
    return path


def _write_config(path: Path, **values: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{k} = "{v}"' for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# safe_completer: never propagates, always returns a list.
# --------------------------------------------------------------------------- #


def test_safe_completer_swallows_exceptions() -> None:
    @completion.safe_completer
    def boom(incomplete: str) -> list[str]:
        raise RuntimeError("completion exploded")

    assert boom("anything") == []


def test_safe_completer_returns_list_from_generator() -> None:
    @completion.safe_completer
    def gen(incomplete: str) -> Iterator[str]:
        yield "a"
        yield "b"

    assert gen("") == ["a", "b"]


# --------------------------------------------------------------------------- #
# Static-source completers.
# --------------------------------------------------------------------------- #


def test_complete_config_key_returns_all_known_keys() -> None:
    items = completion.complete_config_key("")
    values = [item[0] if isinstance(item, tuple) else item for item in items]
    assert set(values) == set(ENV_OVERRIDES)


def test_complete_config_key_prefix_filters() -> None:
    items = completion.complete_config_key("space_")
    values = [item[0] if isinstance(item, tuple) else item for item in items]
    assert values == ["space_display_name", "space_id"]


def test_complete_config_key_includes_env_hint() -> None:
    items = completion.complete_config_key("webhook_url")
    assert ("webhook_url", "env: CGC_WEBHOOK_URL") in items


def test_complete_status_matches_allowed_statuses() -> None:
    assert set(completion.complete_status("")) == set(ALLOWED_STATUSES)


def test_complete_status_prefix_filters() -> None:
    assert completion.complete_status("w") == ["working"]


def test_complete_shell_lists_supported_shells() -> None:
    assert completion.complete_shell("") == ["bash", "fish", "zsh"]


def test_complete_shell_prefix_filters() -> None:
    assert completion.complete_shell("z") == ["zsh"]


# --------------------------------------------------------------------------- #
# Config-derived completers.
# --------------------------------------------------------------------------- #


def test_complete_space_id_from_config(patched_config_path: Path) -> None:
    _write_config(patched_config_path, space_id="spaces/DERIVED")
    assert completion.complete_space_id("") == ["spaces/DERIVED"]


def test_complete_space_id_empty_when_unset(patched_config_path: Path) -> None:
    _write_config(patched_config_path, webhook_url="https://example/x")
    assert completion.complete_space_id("") == []


def test_complete_trigger_prefix_from_config(patched_config_path: Path) -> None:
    _write_config(patched_config_path, trigger_prefix="ops-command:")
    assert completion.complete_trigger_prefix("") == ["ops-command:"]


def test_config_derived_completers_safe_without_file(patched_config_path: Path) -> None:
    # No config file written -> optional-value completers must return [] not raise.
    assert completion.complete_space_id("") == []
    # trigger_prefix has a non-secret default, so it always completes the default.
    from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX

    assert completion.complete_trigger_prefix("") == [DEFAULT_TRIGGER_PREFIX]


def test_config_derived_completers_safe_on_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When Config.load raises (e.g. malformed file), completers return [] not crash.
    def _boom() -> object:
        raise RuntimeError("malformed config")

    monkeypatch.setattr(completion.Config, "load", staticmethod(_boom))
    assert completion._load_config_safely() is None
    assert completion.complete_space_id("") == []
    assert completion.complete_trigger_prefix("") == []


def test_detect_shell_returns_name_or_none() -> None:
    # Delegates to Typer's shell detection; result is a lowercase name or None.
    result = completion.detect_shell()
    assert result is None or result == result.lower()


# --------------------------------------------------------------------------- #
# Completion-script generation.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_render_completion_script_is_non_empty(shell: str) -> None:
    # The completion command must emit a real, non-empty script for bash and zsh.
    script = completion.render_completion_script("cgc", shell)
    assert isinstance(script, str)
    assert script.strip() != ""
    # A meaningful script references the program and its completion env var.
    assert "_CGC_COMPLETE" in script
    assert "cgc" in script


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_render_completion_script_includes_complete_var(shell: str) -> None:
    script = completion.render_completion_script("cgc", shell)
    assert "_CGC_COMPLETE" in script
    assert "cgc" in script


def test_render_completion_script_rejects_unsupported_shell() -> None:
    with pytest.raises(ValueError) as excinfo:
        completion.render_completion_script("cgc", "powershell")
    assert "powershell" in str(excinfo.value)
    assert "bash, zsh, fish" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# rc-file install.
# --------------------------------------------------------------------------- #


def test_rc_path_for_shell_maps_each_shell(tmp_path: Path) -> None:
    assert completion.rc_path_for_shell("bash", home=tmp_path) == tmp_path / ".bashrc"
    assert completion.rc_path_for_shell("zsh", home=tmp_path) == tmp_path / ".zshrc"
    assert (
        completion.rc_path_for_shell("fish", home=tmp_path) == tmp_path / ".config/fish/config.fish"
    )


def test_rc_path_for_shell_rejects_unsupported(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        completion.rc_path_for_shell("powershell", home=tmp_path)


def test_install_completion_line_writes_eval_line(tmp_path: Path) -> None:
    rc = completion.install_completion_line("cgc", "bash", home=tmp_path)
    assert rc == tmp_path / ".bashrc"
    content = rc.read_text(encoding="utf-8")
    # The rc line must request the *source* instruction, never *complete*: the
    # complete instruction reads COMP_WORDS from the env (only set during a TAB)
    # and would dump a traceback at shell start-up.
    assert 'eval "$(env _CGC_COMPLETE=source_bash cgc)"' in content
    assert "complete_bash" not in content
    assert "# cgc shell completion" in content


def test_install_completion_line_is_idempotent(tmp_path: Path) -> None:
    first = completion.install_completion_line("cgc", "zsh", home=tmp_path)
    before = first.read_text(encoding="utf-8")
    completion.install_completion_line("cgc", "zsh", home=tmp_path)
    after = first.read_text(encoding="utf-8")
    assert before == after
    assert after.count("_CGC_COMPLETE=source_zsh") == 1


def test_install_completion_line_preserves_existing_rc(tmp_path: Path) -> None:
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("export EXISTING=1\n", encoding="utf-8")
    completion.install_completion_line("cgc", "bash", home=tmp_path)
    content = rc_path.read_text(encoding="utf-8")
    assert "export EXISTING=1" in content
    assert "_CGC_COMPLETE=source_bash" in content


def test_install_completion_line_fish_uses_source_form(tmp_path: Path) -> None:
    rc = completion.install_completion_line("cgc", "fish", home=tmp_path)
    content = rc.read_text(encoding="utf-8")
    assert "env _CGC_COMPLETE=source_fish cgc | source" in content


def test_install_completion_line_rejects_unsupported(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        completion.install_completion_line("cgc", "powershell", home=tmp_path)
