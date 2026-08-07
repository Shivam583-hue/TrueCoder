from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

HookEvent = Literal["turn_start", "turn_end"]
HookCondition = Literal["always", "files_changed"]

HOOK_EVENTS: Final[tuple[HookEvent, ...]] = ("turn_start", "turn_end")
HOOK_CONDITIONS: Final[tuple[HookCondition, ...]] = ("always", "files_changed")

MAX_HOOKS: Final = 10
MAX_HOOK_NAME_LENGTH: Final = 60
MAX_HOOK_ARGUMENTS: Final = 32
DEFAULT_HOOK_TIMEOUT: Final = 60.0
MAX_HOOK_TIMEOUT: Final = 300.0


class HookConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Hook:
    name: str
    event: HookEvent
    command: tuple[str, ...]
    condition: HookCondition = "always"
    timeout_seconds: float = DEFAULT_HOOK_TIMEOUT
    working_directory: str = "."

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise HookConfigError("a hook requires a name")
        if len(name) > MAX_HOOK_NAME_LENGTH:
            raise HookConfigError(
                f"hook name '{name[:20]}...' exceeds {MAX_HOOK_NAME_LENGTH} characters"
            )
        if self.event not in HOOK_EVENTS:
            raise HookConfigError(
                f"hook '{name}' has an unknown event: {self.event!r}"
            )
        if self.condition not in HOOK_CONDITIONS:
            raise HookConfigError(
                f"hook '{name}' has an unknown condition: {self.condition!r}"
            )
        if self.condition == "files_changed" and self.event != "turn_end":
            raise HookConfigError(
                f"hook '{name}' can only use files_changed with turn_end"
            )
        if not self.command:
            raise HookConfigError(f"hook '{name}' requires a command")
        if len(self.command) > MAX_HOOK_ARGUMENTS:
            raise HookConfigError(
                f"hook '{name}' exceeds {MAX_HOOK_ARGUMENTS} command arguments"
            )
        if not all(
            isinstance(argument, str) and argument for argument in self.command
        ):
            raise HookConfigError(
                f"hook '{name}' has an empty or non-text command argument"
            )
        if not 0 < self.timeout_seconds <= MAX_HOOK_TIMEOUT:
            raise HookConfigError(
                f"hook '{name}' needs a timeout between 0 and {MAX_HOOK_TIMEOUT}"
            )

        object.__setattr__(self, "name", name)

    @property
    def display(self) -> str:
        return " ".join(self.command)


@dataclass(frozen=True, slots=True)
class HookSuite:
    hooks: tuple[Hook, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        names = [hook.name for hook in self.hooks]
        if len(names) != len(set(names)):
            raise HookConfigError("hook names must be unique")
        if len(self.hooks) > MAX_HOOKS:
            raise HookConfigError(f"at most {MAX_HOOKS} hooks are supported")

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def for_event(
        self,
        event: HookEvent,
        *,
        files_changed: bool = False,
    ) -> tuple[Hook, ...]:
        if not self.available:
            return ()
        return tuple(
            hook
            for hook in self.hooks
            if hook.event == event
            and (hook.condition == "always" or files_changed)
        )


@dataclass(frozen=True, slots=True)
class HookOutcome:
    hook: Hook
    status: str
    exit_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.exit_code == 0

    @property
    def summary(self) -> str:
        if self.ok:
            return f"{self.hook.name} ok"
        if self.exit_code is not None:
            return f"{self.hook.name} exited {self.exit_code}"
        return f"{self.hook.name} {self.status}"
