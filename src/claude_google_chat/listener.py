"""Event/poll-driven listener for inbound Google Chat messages.

The listener polls the configured space on an env-driven cadence
(``poll_interval``) and yields newly-seen messages whose text starts with the
configured trigger prefix. The poll interval is a documented cadence, not a
readiness ``sleep``; an idle ``listen_timeout`` (when > 0) causes a fail-fast
non-zero exit with a clear diagnostic. Each emitted message is written to
stdout as a single JSON line (12-factor logs).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from typing import Any

from claude_google_chat.chat import list_messages
from claude_google_chat.config import Config
from claude_google_chat.messages import ChatMessage, parse_message


class ListenerTimeout(RuntimeError):
    """Raised when the listener exceeds its configured idle timeout."""


class Listener:
    """Polls a Google Chat space and yields new trigger-prefixed messages."""

    def __init__(
        self,
        config: Config,
        *,
        fetcher: Callable[[Config, str | None], list[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the listener.

        Args:
            config: Resolved configuration.
            fetcher: Injectable function returning raw Chat messages (defaults
                to :func:`claude_google_chat.chat.list_messages`). Input-driven
                so tests can supply a fake transport.
            clock: Monotonic clock source (injectable for tests).
            sleeper: Cadence sleeper between polls (injectable for tests). This
                paces polling; it is not a readiness wait.
        """
        self._config = config
        self._fetcher = fetcher or (lambda cfg, since: list_messages(cfg, since=since))
        self._clock = clock
        self._sleeper = sleeper
        self._seen: set[str] = set()
        self._since: str | None = None

    def _poll_once(self) -> list[ChatMessage]:
        """Fetch and parse new trigger-prefixed messages since the last poll."""
        raw_messages = self._fetcher(self._config, self._since)
        new: list[ChatMessage] = []
        prefix = self._config.trigger_prefix
        for raw in raw_messages:
            name = raw.get("name", "")
            if name and name in self._seen:
                continue
            if name:
                self._seen.add(name)
            create_time = raw.get("createTime")
            if create_time and (self._since is None or create_time > self._since):
                self._since = create_time
            text = raw.get("text", "")
            if not text.strip().startswith(prefix):
                continue
            new.append(parse_message(text, trigger_prefix=prefix))
        return new

    def iter_new_messages(self, *, once: bool = False) -> Iterator[ChatMessage]:
        """Yield newly-seen trigger-prefixed messages.

        Args:
            once: If true, drain currently-pending messages and stop (for hook
                or CI use). Otherwise poll continuously until the idle timeout
                (when configured) expires.

        Raises:
            ListenerTimeout: If ``listen_timeout`` is > 0 and no new message is
                seen within that window.
        """
        last_emit = self._clock()
        while True:
            batch = self._poll_once()
            if batch:
                last_emit = self._clock()
            yield from batch

            if once:
                return

            timeout = self._config.listen_timeout
            if timeout > 0 and (self._clock() - last_emit) >= timeout:
                raise ListenerTimeout(
                    f"no new Google Chat messages within {timeout}s idle timeout "
                    "(CGC_LISTEN_TIMEOUT); exiting"
                )
            self._sleeper(self._config.poll_interval)


def run(config: Config, *, once: bool = False) -> int:
    """Run the listener, emitting one JSON line per message to stdout.

    Returns a process exit code: 0 on clean completion (``--once`` drain), and
    non-zero on idle timeout (fail fast with a clear diagnostic on stderr).
    """
    listener = Listener(config)
    try:
        for msg in listener.iter_new_messages(once=once):
            sys.stdout.write(json.dumps(asdict(msg), sort_keys=True) + "\n")
            sys.stdout.flush()
    except ListenerTimeout as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0
