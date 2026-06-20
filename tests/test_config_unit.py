"""Exhaustive unit tests for :mod:`claude_google_chat.config`.

Covers the load/merge/write surface end-to-end without touching the real OS
config directory or environment:

- Precedence: explicit env override > config file > non-secret default.
- Defaults for ``trigger_prefix``, ``poll_interval``, ``listen_timeout`` and the
  derived ``token_file`` path.
- Type coercion of numeric values read as TOML/strings.
- ``require_keys`` fail-fast behavior (missing value, unknown key, env hint).
- Secret redaction (``redacted``) and the ``_redact`` boundary lengths.
- The pure ``merge_config_values`` rule and the file-backed
  ``write_config`` / ``merge_and_write_config`` round-trips, including
  fail-fast on unknown keys.

Everything is input-driven via the shared fixtures (``config_path``,
``write_config_file``, ``make_config``) so no test reads the host environment.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from claude_google_chat.config import (
    DEFAULT_LISTEN_TIMEOUT,
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REQUIRE_TRIGGER,
    DEFAULT_SEND_ENVELOPE,
    ENV_OVERRIDES,
    Config,
    _parse_bool,
    _redact,
    default_state_path,
    default_token_path,
    merge_and_write_config,
    merge_config_values,
    write_config,
)
from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX

# --------------------------------------------------------------------------- #
# load(): defaults and the derived token path.
# --------------------------------------------------------------------------- #


def test_load_missing_file_is_empty_env_only(tmp_path: Path) -> None:
    """A non-existent file is treated as empty config (no error)."""
    missing = tmp_path / "does-not-exist.toml"
    assert not missing.exists()
    config = Config.load(path=missing, env={})
    assert config.webhook_url is None
    assert config.space_id is None
    assert config.trigger_prefix == DEFAULT_TRIGGER_PREFIX
    assert config.poll_interval == DEFAULT_POLL_INTERVAL
    assert config.listen_timeout == DEFAULT_LISTEN_TIMEOUT


def test_load_applies_non_secret_defaults(write_config_file: Callable[..., Path]) -> None:
    """When only secrets are set, tunables fall back to documented defaults."""
    path = write_config_file(webhook_url="https://example/x")
    config = Config.load(path=path, env={})
    assert config.trigger_prefix == DEFAULT_TRIGGER_PREFIX
    assert config.poll_interval == DEFAULT_POLL_INTERVAL
    assert config.listen_timeout == DEFAULT_LISTEN_TIMEOUT


def test_token_file_defaults_to_config_dir_path(write_config_file: Callable[..., Path]) -> None:
    """An unset ``token_file`` resolves to the default cached-token path."""
    path = write_config_file(space_id="spaces/AAAA")
    config = Config.load(path=path, env={})
    assert config.token_file == str(default_token_path())


def test_token_file_explicit_value_wins(write_config_file: Callable[..., Path]) -> None:
    """An explicit ``token_file`` in the file is preserved verbatim."""
    path = write_config_file(token_file="/custom/token.json")
    config = Config.load(path=path, env={})
    assert config.token_file == "/custom/token.json"


# --------------------------------------------------------------------------- #
# load(): resilience + catch-all + state-file defaults and coercion.
# --------------------------------------------------------------------------- #


def test_resilience_and_catch_all_defaults_applied() -> None:
    """The new tunables fall back to their documented defaults when unset."""
    config = Config.load(path=Path("/nonexistent.toml"), env={})
    assert config.max_consecutive_errors == DEFAULT_MAX_CONSECUTIVE_ERRORS
    assert config.require_trigger is DEFAULT_REQUIRE_TRIGGER
    assert config.require_trigger is True


def test_state_file_defaults_to_config_dir_path(write_config_file: Callable[..., Path]) -> None:
    """An unset ``state_file`` resolves to the default listen-state path."""
    path = write_config_file(space_id="spaces/AAAA")
    config = Config.load(path=path, env={})
    assert config.state_file == str(default_state_path())


def test_state_file_explicit_value_wins(write_config_file: Callable[..., Path]) -> None:
    path = write_config_file(state_file="/custom/listen-state.json")
    config = Config.load(path=path, env={})
    assert config.state_file == "/custom/listen-state.json"


def test_max_consecutive_errors_coerced_to_int_from_env() -> None:
    config = Config.load(path=Path("/nonexistent.toml"), env={"CGC_MAX_CONSECUTIVE_ERRORS": "25"})
    assert config.max_consecutive_errors == 25
    assert isinstance(config.max_consecutive_errors, int)


def test_max_consecutive_errors_invalid_env_fails_fast() -> None:
    with pytest.raises(ValueError):
        Config.load(
            path=Path("/nonexistent.toml"), env={"CGC_MAX_CONSECUTIVE_ERRORS": "not-an-int"}
        )


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_require_trigger_falsey_env_parses_false(raw: str) -> None:
    config = Config.load(path=Path("/nonexistent.toml"), env={"CGC_REQUIRE_TRIGGER": raw})
    assert config.require_trigger is False


def test_require_trigger_unparseable_env_fails_fast() -> None:
    with pytest.raises(ValueError):
        Config.load(path=Path("/nonexistent.toml"), env={"CGC_REQUIRE_TRIGGER": "maybe"})


# --------------------------------------------------------------------------- #
# load(): file values and numeric coercion.
# --------------------------------------------------------------------------- #


def test_load_reads_all_string_fields_from_file(write_config_file: Callable[..., Path]) -> None:
    path = write_config_file(
        webhook_url="https://hook/x",
        space_id="spaces/AAAA",
        oauth_client_file="/c.json",
        token_file="/t.json",
        trigger_prefix="ops:",
    )
    config = Config.load(path=path, env={})
    assert config.webhook_url == "https://hook/x"
    assert config.space_id == "spaces/AAAA"
    assert config.oauth_client_file == "/c.json"
    assert config.token_file == "/t.json"
    assert config.trigger_prefix == "ops:"


def test_load_coerces_numeric_tunables_to_float(write_config_file: Callable[..., Path]) -> None:
    """``poll_interval`` / ``listen_timeout`` are always floats."""
    path = write_config_file(poll_interval=5.0, listen_timeout=30.0)
    config = Config.load(path=path, env={})
    assert isinstance(config.poll_interval, float)
    assert config.poll_interval == 5.0
    assert isinstance(config.listen_timeout, float)
    assert config.listen_timeout == 30.0


def test_load_coerces_integer_string_env_to_float() -> None:
    """An integer-looking env value still coerces to float (no fallback)."""
    env = {"CGC_POLL_INTERVAL": "7", "CGC_LISTEN_TIMEOUT": "0"}
    config = Config.load(path=Path("/nonexistent.toml"), env=env)
    assert config.poll_interval == 7.0
    assert config.listen_timeout == 0.0


def test_load_invalid_numeric_env_fails_fast() -> None:
    """A non-numeric tunable raises rather than silently defaulting."""
    env = {"CGC_POLL_INTERVAL": "not-a-number"}
    with pytest.raises(ValueError):
        Config.load(path=Path("/nonexistent.toml"), env=env)


# --------------------------------------------------------------------------- #
# load(): send_envelope boolean coercion (default, env, file, fail-fast).
# --------------------------------------------------------------------------- #


def test_send_envelope_defaults_to_false() -> None:
    """The human-facing Chat view is clean by default (envelope opt-in)."""
    config = Config.load(path=Path("/nonexistent.toml"), env={})
    assert config.send_envelope is DEFAULT_SEND_ENVELOPE
    assert config.send_envelope is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", "ON"])
def test_send_envelope_truthy_env_values_parse_true(raw: str) -> None:
    config = Config.load(path=Path("/nonexistent.toml"), env={"CGC_SEND_ENVELOPE": raw})
    assert config.send_envelope is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", "off", "OFF"])
def test_send_envelope_falsey_env_values_parse_false(raw: str) -> None:
    config = Config.load(path=Path("/nonexistent.toml"), env={"CGC_SEND_ENVELOPE": raw})
    assert config.send_envelope is False


def test_send_envelope_unparseable_env_fails_fast() -> None:
    """An unrecognised boolean spelling raises rather than silently defaulting."""
    env = {"CGC_SEND_ENVELOPE": "maybe"}
    with pytest.raises(ValueError) as exc_info:
        Config.load(path=Path("/nonexistent.toml"), env=env)
    assert "boolean" in str(exc_info.value).lower()


def test_send_envelope_reads_typed_bool_from_toml(
    write_config_file: Callable[..., Path],
) -> None:
    """A TOML boolean (already typed) is honoured without coercion errors."""
    path = write_config_file(send_envelope=True)
    config = Config.load(path=path, env={})
    assert config.send_envelope is True


def test_parse_bool_accepts_native_bool() -> None:
    assert _parse_bool(True) is True
    assert _parse_bool(False) is False


def test_parse_bool_rejects_unparseable_value() -> None:
    with pytest.raises(ValueError):
        _parse_bool("definitely-not-a-bool")


# --------------------------------------------------------------------------- #
# load(): precedence (env over file, empty env ignored).
# --------------------------------------------------------------------------- #


def test_env_overrides_file_value(write_config_file: Callable[..., Path]) -> None:
    path = write_config_file(trigger_prefix="file-command:")
    config = Config.load(path=path, env={"CGC_TRIGGER_PREFIX": "env-command:"})
    assert config.trigger_prefix == "env-command:"


def test_empty_env_value_does_not_override_file(write_config_file: Callable[..., Path]) -> None:
    """An empty-string env var is ignored; the file value stands."""
    path = write_config_file(space_id="spaces/FROMFILE")
    config = Config.load(path=path, env={"CGC_SPACE_ID": ""})
    assert config.space_id == "spaces/FROMFILE"


def test_empty_env_value_with_no_file_yields_none() -> None:
    """An empty-string env var with no file leaves the field unset."""
    config = Config.load(path=Path("/nonexistent.toml"), env={"CGC_SPACE_ID": ""})
    assert config.space_id is None


def test_env_supplies_value_absent_from_file() -> None:
    config = Config.load(
        path=Path("/nonexistent.toml"),
        env={"CGC_OAUTH_CLIENT_FILE": "/client.json"},
    )
    assert config.oauth_client_file == "/client.json"


def test_every_env_override_is_honoured() -> None:
    """Each declared env var maps onto its config field."""
    # Typed fields need parseable values; everything else takes a string marker.
    typed_fields = {
        "poll_interval",
        "listen_timeout",
        "webhook_timeout",
        "page_size",
        "send_envelope",
        "max_consecutive_errors",
        "require_trigger",
        "threads",
    }
    env = {var: f"value-for-{key}" for key, var in ENV_OVERRIDES.items() if key not in typed_fields}
    env["CGC_POLL_INTERVAL"] = "3.5"
    env["CGC_LISTEN_TIMEOUT"] = "12.0"
    env["CGC_WEBHOOK_TIMEOUT"] = "45.0"
    env["CGC_PAGE_SIZE"] = "250"
    env["CGC_SEND_ENVELOPE"] = "true"
    env["CGC_MAX_CONSECUTIVE_ERRORS"] = "7"
    env["CGC_REQUIRE_TRIGGER"] = "false"
    env["CGC_THREADS"] = "spaces/AAAA/threads/T1, spaces/AAAA/threads/T2"
    config = Config.load(path=Path("/nonexistent.toml"), env=env)
    for key in ENV_OVERRIDES:
        if key in typed_fields:
            continue
        value = getattr(config, key)
        assert value == f"value-for-{key}", key
    assert config.poll_interval == 3.5
    assert config.listen_timeout == 12.0
    assert config.webhook_timeout == 45.0
    assert config.page_size == 250
    assert config.send_envelope is True
    assert config.max_consecutive_errors == 7
    assert config.require_trigger is False
    assert config.threads == ("spaces/AAAA/threads/T1", "spaces/AAAA/threads/T2")


# --------------------------------------------------------------------------- #
# require_keys / load(require=...): fail-fast on missing required values.
# --------------------------------------------------------------------------- #


def test_load_require_missing_raises_with_key_name(write_config_file: Callable[..., Path]) -> None:
    path = write_config_file(webhook_url="https://hook/x")
    with pytest.raises(ValueError) as exc_info:
        Config.load(path=path, env={}, require=("space_id",))
    assert "space_id" in str(exc_info.value)


def test_require_keys_present_does_not_raise(make_config: Callable[..., Config]) -> None:
    config = make_config(space_id="spaces/AAAA", webhook_url="https://hook/x")
    # Should not raise.
    config.require_keys(("space_id", "webhook_url"))


def test_require_keys_empty_string_is_missing(make_config: Callable[..., Config]) -> None:
    config = make_config(space_id="")
    with pytest.raises(ValueError) as exc_info:
        config.require_keys(("space_id",))
    assert "space_id" in str(exc_info.value)


def test_require_keys_none_is_missing(make_config: Callable[..., Config]) -> None:
    config = make_config(oauth_client_file=None)
    with pytest.raises(ValueError):
        config.require_keys(("oauth_client_file",))


def test_require_keys_error_includes_env_hint(make_config: Callable[..., Config]) -> None:
    """The fail-fast message names the env var to make it actionable."""
    config = make_config(space_id=None)
    with pytest.raises(ValueError) as exc_info:
        config.require_keys(("space_id",))
    assert ENV_OVERRIDES["space_id"] in str(exc_info.value)


def test_require_keys_unknown_key_raises(make_config: Callable[..., Config]) -> None:
    config = make_config()
    with pytest.raises(ValueError) as exc_info:
        config.require_keys(("not_a_field",))
    assert "not_a_field" in str(exc_info.value)


def test_require_keys_empty_tuple_is_noop(make_config: Callable[..., Config]) -> None:
    config = make_config()
    # No requirement -> no raise even with otherwise-empty fields.
    config.require_keys(())


# --------------------------------------------------------------------------- #
# redacted() / _redact(): secrets masked, non-secrets untouched.
# --------------------------------------------------------------------------- #


def test_redacted_masks_all_secret_keys(make_config: Callable[..., Config]) -> None:
    config = make_config(
        webhook_url="https://hook/x?key=SUPERSECRETKEYVALUE",
        token_file="/secret/token-file-path.json",
    )
    redacted = config.redacted()
    assert "SUPERSECRETKEYVALUE" not in str(redacted["webhook_url"])
    assert redacted["token_file"] != config.token_file


def test_redacted_preserves_non_secret_fields(make_config: Callable[..., Config]) -> None:
    config = make_config(
        space_id="spaces/AAAA",
        trigger_prefix="claude:",
        oauth_client_file="/client.json",
    )
    redacted = config.redacted()
    assert redacted["space_id"] == "spaces/AAAA"
    assert redacted["trigger_prefix"] == "claude:"
    assert redacted["oauth_client_file"] == "/client.json"


def test_redacted_leaves_empty_secret_unchanged(make_config: Callable[..., Config]) -> None:
    """An unset secret stays falsy rather than rendering ``***``."""
    config = make_config(webhook_url=None)
    assert config.redacted()["webhook_url"] is None


def test_redact_short_value_fully_masked() -> None:
    assert _redact("short") == "***"


def test_redact_boundary_eight_chars_fully_masked() -> None:
    """At exactly 8 chars the value is still fully masked (<= 8 rule)."""
    assert _redact("12345678") == "***"


def test_redact_long_value_keeps_edges_only() -> None:
    result = _redact("ABCDwowmiddleEFGH")
    assert result.startswith("ABCD")
    assert result.endswith("EFGH")
    assert "wowmiddle" not in result


def test_redact_empty_value_returns_empty() -> None:
    assert _redact("") == ""


# --------------------------------------------------------------------------- #
# merge_config_values(): pure merge rule.
# --------------------------------------------------------------------------- #


def test_merge_updates_overwrite_existing() -> None:
    merged = merge_config_values(
        {"space_id": "spaces/OLD", "trigger_prefix": "p:"},
        {"space_id": "spaces/NEW"},
    )
    assert merged["space_id"] == "spaces/NEW"
    assert merged["trigger_prefix"] == "p:"


def test_merge_skips_none_updates_preserving_existing() -> None:
    merged = merge_config_values(
        {"space_id": "spaces/KEEP"},
        {"space_id": None, "trigger_prefix": "p:"},
    )
    assert merged["space_id"] == "spaces/KEEP"
    assert merged["trigger_prefix"] == "p:"


def test_merge_drops_none_existing_values() -> None:
    """``None`` values already in ``existing`` are pruned from the result."""
    merged = merge_config_values({"space_id": None, "trigger_prefix": "p:"}, {})
    assert "space_id" not in merged
    assert merged["trigger_prefix"] == "p:"


def test_merge_rejects_unknown_existing_key() -> None:
    with pytest.raises(ValueError) as exc_info:
        merge_config_values({"bogus": "x"}, {})
    assert "bogus" in str(exc_info.value)


def test_merge_rejects_unknown_update_key() -> None:
    with pytest.raises(ValueError) as exc_info:
        merge_config_values({}, {"bogus": "x"})
    assert "bogus" in str(exc_info.value)


def test_merge_empty_inputs_yield_empty() -> None:
    assert merge_config_values({}, {}) == {}


# --------------------------------------------------------------------------- #
# write_config(): persistence + permissions + fail-fast.
# --------------------------------------------------------------------------- #


def test_write_config_round_trips_via_load(config_path: Path) -> None:
    write_config(
        {"space_id": "spaces/A", "trigger_prefix": "p:", "poll_interval": 4.0},
        path=config_path,
    )
    reloaded = Config.load(path=config_path, env={})
    assert reloaded.space_id == "spaces/A"
    assert reloaded.trigger_prefix == "p:"
    assert reloaded.poll_interval == 4.0


def test_write_config_skips_none_values(config_path: Path) -> None:
    write_config({"space_id": "spaces/A", "trigger_prefix": None}, path=config_path)
    contents = config_path.read_text(encoding="utf-8")
    assert "space_id" in contents
    assert "trigger_prefix" not in contents


def test_write_config_sets_owner_only_permissions(config_path: Path) -> None:
    write_config({"space_id": "spaces/A"}, path=config_path)
    mode = config_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_config_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "config.toml"
    assert not nested.parent.exists()
    write_config({"space_id": "spaces/A"}, path=nested)
    assert nested.exists()


def test_write_config_rejects_unknown_key(config_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        write_config({"bogus": "x"}, path=config_path)
    assert "bogus" in str(exc_info.value)


def test_write_config_escapes_quotes_and_backslashes(config_path: Path) -> None:
    """Values containing quotes/backslashes round-trip through TOML reads."""
    value = 'a "quoted" \\path\\ thing'
    write_config({"trigger_prefix": value}, path=config_path)
    reloaded = Config.load(path=config_path, env={})
    assert reloaded.trigger_prefix == value


# --------------------------------------------------------------------------- #
# merge_and_write_config(): read-modify-write idempotence.
# --------------------------------------------------------------------------- #


def test_merge_and_write_preserves_prior_values(config_path: Path) -> None:
    merge_and_write_config({"space_id": "spaces/A", "trigger_prefix": "p:"}, path=config_path)
    merge_and_write_config({"space_id": "spaces/B"}, path=config_path)
    reloaded = Config.load(path=config_path, env={})
    assert reloaded.space_id == "spaces/B"
    assert reloaded.trigger_prefix == "p:"


def test_merge_and_write_on_missing_file_starts_empty(config_path: Path) -> None:
    assert not config_path.exists()
    merge_and_write_config({"space_id": "spaces/NEW"}, path=config_path)
    reloaded = Config.load(path=config_path, env={})
    assert reloaded.space_id == "spaces/NEW"


def test_merge_and_write_none_update_keeps_existing(config_path: Path) -> None:
    merge_and_write_config({"trigger_prefix": "keep-me:"}, path=config_path)
    merge_and_write_config({"trigger_prefix": None, "space_id": "spaces/X"}, path=config_path)
    reloaded = Config.load(path=config_path, env={})
    assert reloaded.trigger_prefix == "keep-me:"
    assert reloaded.space_id == "spaces/X"


def test_merge_and_write_rejects_unknown_update(config_path: Path) -> None:
    with pytest.raises(ValueError):
        merge_and_write_config({"bogus": "x"}, path=config_path)
