"""Configuration loading and persistence.

Precedence (highest first): explicit value -> environment variable -> user
config file -> error if a required value is missing. Secrets have no defaults
and are never echoed in cleartext. The user config file lives under the OS
config directory (never inside the repo or CWD).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from platformdirs import user_config_path

from claude_google_chat.messages import DEFAULT_TRIGGER_PREFIX

APP_NAME = "claude-google-chat"

# Non-secret tunable defaults (documented in docs/configuration.md).
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_LISTEN_TIMEOUT = 0.0  # 0 == run forever
DEFAULT_WEBHOOK_TIMEOUT = 30.0  # seconds; outbound webhook HTTP timeout
DEFAULT_PAGE_SIZE = 100  # Chat API messages.list page size
DEFAULT_SEND_ENVELOPE = False  # opt-in: append the JSON envelope to outbound Chat text
# Consecutive transient poll failures tolerated before the loop fails fast. A
# truly-down backend still surfaces (non-zero exit) once this bound is reached.
DEFAULT_MAX_CONSECUTIVE_ERRORS = 10
DEFAULT_REQUIRE_TRIGGER = True  # default: only emit trigger-prefixed messages

# Accepted string spellings when coercing a boolean config value (TOML booleans
# arrive already typed; env-var / string values are matched case-insensitively).
# Single source of truth for the bool parser below.
_TRUTHY_STRINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSEY_STRINGS: frozenset[str] = frozenset({"0", "false", "no", "off"})

# Mapping of config keys to their environment-variable overrides.
ENV_OVERRIDES: dict[str, str] = {
    "webhook_url": "CGC_WEBHOOK_URL",
    "space_id": "CGC_SPACE_ID",
    "oauth_client_file": "CGC_OAUTH_CLIENT_FILE",
    "token_file": "CGC_TOKEN_FILE",
    "trigger_prefix": "CGC_TRIGGER_PREFIX",
    "poll_interval": "CGC_POLL_INTERVAL",
    "listen_timeout": "CGC_LISTEN_TIMEOUT",
    "webhook_timeout": "CGC_WEBHOOK_TIMEOUT",
    "page_size": "CGC_PAGE_SIZE",
    "send_envelope": "CGC_SEND_ENVELOPE",
    "max_consecutive_errors": "CGC_MAX_CONSECUTIVE_ERRORS",
    "state_file": "CGC_STATE_FILE",
    "require_trigger": "CGC_REQUIRE_TRIGGER",
    "threads": "CGC_THREADS",
    "sessions_file": "CGC_SESSIONS_FILE",
}

_SECRET_KEYS: frozenset[str] = frozenset({"webhook_url", "token_file"})


def config_dir() -> Path:
    """Return the OS-specific configuration directory for this app."""
    return user_config_path(APP_NAME)


def default_config_path() -> Path:
    """Return the default path to ``config.toml`` under the config dir."""
    return config_dir() / "config.toml"


def default_token_path() -> Path:
    """Return the default cached OAuth token path under the config dir."""
    return config_dir() / "token.json"


def default_state_path() -> Path:
    """Return the default durable listener-state path under the config dir.

    Holds the last-processed high-water marker so a restart resumes instead of
    re-reading recent history and re-emitting already-seen messages.
    """
    return config_dir() / "listen-state.json"


def default_sessions_path() -> Path:
    """Return the default durable session-registry path under the config dir.

    Holds the local session map (name → space, claimed threads, dispatcher flag,
    created_at) used by ``cgc connect``/``list``/``disconnect`` and routing-aware
    ``cgc listen``. Written with owner-only (``0600``) permissions.
    """
    return config_dir() / "sessions.json"


def _parse_bool(value: object) -> bool:
    """Coerce a config value to ``bool``, failing fast on anything unparseable.

    Accepts an actual ``bool`` (as TOML yields), or one of the case-insensitive
    string spellings in :data:`_TRUTHY_STRINGS` / :data:`_FALSEY_STRINGS` (as an
    environment variable yields). Any other value raises ``ValueError`` with an
    actionable message rather than silently defaulting — mirroring the fail-fast
    behaviour of the numeric (``float``/``int``) coercions in :meth:`Config.load`.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY_STRINGS:
        return True
    if text in _FALSEY_STRINGS:
        return False
    allowed = ", ".join(sorted(_TRUTHY_STRINGS | _FALSEY_STRINGS))
    raise ValueError(f"invalid boolean config value {value!r}; expected one of: {allowed}")


def _parse_threads(value: object) -> tuple[str, ...]:
    """Coerce a ``threads`` config value to a tuple of thread resource names.

    Accepts a TOML array of strings (as the config file yields) or a single
    comma-separated string (as ``CGC_THREADS`` yields). Whitespace around each
    entry is trimmed and empty entries are dropped, so ``"a, ,b"`` and
    ``["a", "b"]`` both yield ``("a", "b")``. Any other shape (e.g. a non-string
    list element) raises ``ValueError`` (fail fast) rather than being silently
    coerced. An empty result is returned as an empty tuple (no thread filter).
    """
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for element in value:
            if not isinstance(element, str):
                raise ValueError(
                    f"invalid threads entry {element!r}; expected a string thread resource name"
                )
            trimmed = element.strip()
            if trimmed:
                items.append(trimmed)
        return tuple(items)
    text = str(value)
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _redact(value: str) -> str:
    """Redact a secret value, keeping only a short non-sensitive hint."""
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


@dataclass(frozen=True)
class Config:
    """Resolved, validated configuration.

    Built via :meth:`load`, which merges the config file and environment and
    applies non-secret defaults. Required secrets have no defaults.
    """

    webhook_url: str | None = None
    space_id: str | None = None
    oauth_client_file: str | None = None
    token_file: str | None = None
    trigger_prefix: str = DEFAULT_TRIGGER_PREFIX
    poll_interval: float = DEFAULT_POLL_INTERVAL
    listen_timeout: float = DEFAULT_LISTEN_TIMEOUT
    webhook_timeout: float = DEFAULT_WEBHOOK_TIMEOUT
    page_size: int = DEFAULT_PAGE_SIZE
    send_envelope: bool = DEFAULT_SEND_ENVELOPE
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS
    state_file: str | None = None
    require_trigger: bool = DEFAULT_REQUIRE_TRIGGER
    threads: tuple[str, ...] = ()
    sessions_file: str | None = None

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        require: tuple[str, ...] = (),
    ) -> Config:
        """Load configuration from a TOML file merged with environment vars.

        Args:
            path: TOML file to read; defaults to :func:`default_config_path`.
                A missing file is treated as empty (env-only) configuration.
            env: Environment mapping to read overrides from; defaults to
                ``os.environ`` (injectable for tests — input-driven).
            require: Keys that must resolve to a non-empty value. If any are
                missing the call raises ``ValueError`` (fail fast).

        Returns:
            A frozen :class:`Config`.
        """
        resolved_env: Mapping[str, str] = os.environ if env is None else env
        file_path = default_config_path() if path is None else path

        file_data: dict[str, object] = {}
        if file_path.exists():
            file_data = tomllib.loads(file_path.read_text(encoding="utf-8"))

        merged: dict[str, object] = {}
        for key, env_var in ENV_OVERRIDES.items():
            if env_var in resolved_env and resolved_env[env_var] != "":
                merged[key] = resolved_env[env_var]
            elif key in file_data:
                merged[key] = file_data[key]

        def _opt_str(key: str) -> str | None:
            return str(merged[key]) if key in merged else None

        token_file = (
            str(merged["token_file"]) if "token_file" in merged else str(default_token_path())
        )
        state_file = (
            str(merged["state_file"]) if "state_file" in merged else str(default_state_path())
        )
        sessions_file = (
            str(merged["sessions_file"])
            if "sessions_file" in merged
            else str(default_sessions_path())
        )
        config = cls(
            webhook_url=_opt_str("webhook_url"),
            space_id=_opt_str("space_id"),
            oauth_client_file=_opt_str("oauth_client_file"),
            token_file=token_file,
            trigger_prefix=(
                str(merged["trigger_prefix"])
                if "trigger_prefix" in merged
                else DEFAULT_TRIGGER_PREFIX
            ),
            poll_interval=(
                float(str(merged["poll_interval"]))
                if "poll_interval" in merged
                else DEFAULT_POLL_INTERVAL
            ),
            listen_timeout=(
                float(str(merged["listen_timeout"]))
                if "listen_timeout" in merged
                else DEFAULT_LISTEN_TIMEOUT
            ),
            webhook_timeout=(
                float(str(merged["webhook_timeout"]))
                if "webhook_timeout" in merged
                else DEFAULT_WEBHOOK_TIMEOUT
            ),
            page_size=(
                int(str(merged["page_size"])) if "page_size" in merged else DEFAULT_PAGE_SIZE
            ),
            send_envelope=(
                _parse_bool(merged["send_envelope"])
                if "send_envelope" in merged
                else DEFAULT_SEND_ENVELOPE
            ),
            max_consecutive_errors=(
                int(str(merged["max_consecutive_errors"]))
                if "max_consecutive_errors" in merged
                else DEFAULT_MAX_CONSECUTIVE_ERRORS
            ),
            state_file=state_file,
            require_trigger=(
                _parse_bool(merged["require_trigger"])
                if "require_trigger" in merged
                else DEFAULT_REQUIRE_TRIGGER
            ),
            threads=(_parse_threads(merged["threads"]) if "threads" in merged else ()),
            sessions_file=sessions_file,
        )
        config.require_keys(require)
        return config

    def require_keys(self, keys: tuple[str, ...]) -> None:
        """Raise ``ValueError`` if any required key resolves to empty.

        Fails fast and names the missing key so the error is actionable.
        """
        valid = {f.name for f in fields(self)}
        for key in keys:
            if key not in valid:
                raise ValueError(f"unknown required config key {key!r}")
            value = getattr(self, key)
            if value is None or value == "":
                env_var = ENV_OVERRIDES.get(key, "")
                hint = f" (set {env_var} or add it to config.toml)" if env_var else ""
                raise ValueError(f"missing required config value {key!r}{hint}")

    def redacted(self) -> dict[str, object]:
        """Return a dict view of the config with secrets masked.

        Used by ``cgc config show`` so secrets are never echoed in cleartext.
        """
        result: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _SECRET_KEYS and isinstance(value, str) and value:
                result[f.name] = _redact(value)
            else:
                result[f.name] = value
        return result


def _toml_string(value: object) -> str:
    """Serialise a value to a minimal double-quoted TOML basic string."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: object) -> str:
    """Serialise a scalar (or list-of-strings) value to a minimal TOML literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_string(element) for element in value) + "]"
    return _toml_string(value)


def merge_config_values(
    existing: Mapping[str, object],
    updates: Mapping[str, object],
) -> dict[str, object]:
    """Merge ``updates`` over ``existing`` config values (pure, no I/O).

    Keys present in ``updates`` with a non-``None`` value overwrite the matching
    key in ``existing``; ``None`` values in ``updates`` are skipped so a caller
    that does not know a value leaves any prior value intact. Every resulting
    key must be a known config key (validated against :data:`ENV_OVERRIDES`),
    failing fast on an unknown key. This is the single, testable merge rule used
    by ``cgc config set`` so partial updates never drop previously-stored
    settings.
    """
    valid = set(ENV_OVERRIDES)
    merged: dict[str, object] = {}
    for key, value in existing.items():
        if key not in valid:
            raise ValueError(f"unknown config key {key!r}")
        if value is not None:
            merged[key] = value
    for key, value in updates.items():
        if key not in valid:
            raise ValueError(f"unknown config key {key!r}")
        if value is None:
            continue
        merged[key] = value
    return merged


def write_config(values: dict[str, object], path: Path | None = None) -> Path:
    """Persist config ``values`` to a TOML file under the config dir.

    Uses a minimal stdlib serialiser (no third-party TOML writer needed),
    while reads go through ``tomllib``. Returns the path written.
    """
    file_path = default_config_path() if path is None else path
    file_path.parent.mkdir(parents=True, exist_ok=True)

    valid = {name for name in ENV_OVERRIDES}
    lines = ["# claude-google-chat configuration", ""]
    for key, value in values.items():
        if key not in valid:
            raise ValueError(f"unknown config key {key!r}")
        if value is None:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_path.chmod(0o600)
    return file_path


def merge_and_write_config(
    updates: Mapping[str, object],
    path: Path | None = None,
) -> Path:
    """Read the existing config (if any), merge ``updates``, and persist it.

    Returns the path written. Used by ``cgc config set`` to merge a single key
    into ``config.toml`` without clobbering values the user set earlier (e.g.
    ``trigger_prefix``).
    """
    file_path = default_config_path() if path is None else path
    existing: dict[str, object] = {}
    if file_path.exists():
        existing = dict(tomllib.loads(file_path.read_text(encoding="utf-8")))
    merged = merge_config_values(existing, updates)
    return write_config(merged, path=file_path)
