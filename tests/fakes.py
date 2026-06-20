"""Injectable fakes for the onboarding (``setup``) and diagnostics (``doctor``) tests.

These drive every external boundary the wizard/doctor touch — running gcloud,
probing the Chat API, the clock, the cadence sleeper, and user interaction — with
in-process, scripted behaviour. No subprocess, network, real disk, or real sleep
is ever involved, so the tests are fast and hermetic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from claude_google_chat.config import Config
from claude_google_chat.probes import CommandResult, Probes


class FakeCommandRunner:
    """Scripted :class:`~claude_google_chat.probes.CommandRunner`.

    ``responses`` maps a stringified argv (joined by spaces) to a
    :class:`CommandResult`; ``which_map`` maps a program name to its resolved
    path (or ``None``). Every ``run`` is recorded in ``calls`` for assertions. A
    callable response value is invoked with the argv so a test can vary a result
    across repeated calls (e.g. "not enabled, then enabled" for the poll).
    """

    def __init__(
        self,
        *,
        responses: dict[str, object] | None = None,
        which_map: dict[str, str | None] | None = None,
        default: CommandResult | None = None,
    ) -> None:
        self._responses = responses or {}
        self._which = which_map or {}
        self._default = default if default is not None else CommandResult(returncode=0)
        self.calls: list[list[str]] = []

    def run(self, args: Sequence[str]) -> CommandResult:
        argv = list(args)
        self.calls.append(argv)
        key = " ".join(argv)
        # Longest-prefix match so a test can key on a command stem.
        for candidate in sorted(self._responses, key=len, reverse=True):
            if key == candidate or key.startswith(candidate + " "):
                value = self._responses[candidate]
                if callable(value):
                    return value(argv)
                assert isinstance(value, CommandResult)
                return value
        return self._default

    def which(self, program: str) -> str | None:
        return self._which.get(program, f"/usr/bin/{program}")


class FakeChatProbe:
    """Scripted :class:`~claude_google_chat.probes.ChatProbe`.

    ``scopes`` is returned by ``token_scopes`` (or, if it is an ``Exception``,
    raised to model unreadable/refused credentials). ``roundtrip`` controls
    ``send_and_read_back`` (a bool result, or an ``Exception`` to raise). Calls
    are recorded so tests assert the round-trip gate ran.
    """

    def __init__(
        self,
        *,
        scopes: list[str] | Exception | None = None,
        roundtrip: bool | Exception = True,
    ) -> None:
        self._scopes = scopes
        self._roundtrip = roundtrip
        self.scope_calls = 0
        self.roundtrip_markers: list[str] = []

    def token_scopes(self, config: Config) -> list[str]:
        self.scope_calls += 1
        if isinstance(self._scopes, Exception):
            raise self._scopes
        return list(self._scopes) if self._scopes else []

    def send_and_read_back(self, config: Config, marker: str) -> bool:
        self.roundtrip_markers.append(marker)
        if isinstance(self._roundtrip, Exception):
            raise self._roundtrip
        return self._roundtrip


class FakeClock:
    """Monotonic clock that advances by ``step`` each time ``sleep`` is called.

    Lets the readiness poll terminate deterministically without real waiting:
    ``now`` reads the current value; ``sleep`` advances it by ``step`` so a
    bounded poll either succeeds or times out after a finite number of iterations.
    """

    def __init__(self, *, step: float = 1.0) -> None:
        self._t = 0.0
        self._step = step
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._t += self._step


@dataclass
class ScriptedIO:
    """Scripted user-interaction channel for the wizard.

    ``answers`` are dequeued by ``prompt``; ``confirms`` by ``confirm``; every
    emitted line is appended to ``lines`` for assertions.
    """

    answers: list[str] = field(default_factory=list)
    confirms: list[bool] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def prompt(self, message: str) -> str:
        assert self.answers, f"unexpected prompt with no scripted answer: {message!r}"
        return self.answers.pop(0)

    def confirm(self, message: str) -> bool:
        assert self.confirms, f"unexpected confirm with no scripted answer: {message!r}"
        return self.confirms.pop(0)

    def emit(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        """All emitted lines joined, for substring assertions."""
        return "\n".join(self.lines)


def make_probes(
    *,
    runner: FakeCommandRunner | None = None,
    chat: FakeChatProbe | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Probes:
    """Assemble a :class:`Probes` bundle from fakes, defaulting each boundary."""
    fake_clock = FakeClock()
    return Probes(
        runner=runner if runner is not None else FakeCommandRunner(),
        chat=chat if chat is not None else FakeChatProbe(),
        env=env if env is not None else {},
        clock=clock if clock is not None else fake_clock.now,
        sleeper=sleeper if sleeper is not None else fake_clock.sleep,
    )
