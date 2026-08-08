from __future__ import annotations

from dataclasses import dataclass
from typing import Final

COMMAND_PREFIX: Final = "/"
MAX_COMMAND_CHARACTERS: Final = 64


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    summary: str

    def __post_init__(self) -> None:
        if not self.name.strip() or " " in self.name:
            raise ValueError("a command name must be one word")
        if not self.summary.strip():
            raise ValueError(f"command {self.name!r} needs a summary")

    @property
    def invocation(self) -> str:
        return f"{COMMAND_PREFIX}{self.name}"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    argument: str


COMMANDS: Final = (
    SlashCommand("models", "Choose which model answers"),
    SlashCommand("model", "Show the model currently answering"),
    SlashCommand("help", "List what you can type here"),
)

_BY_NAME: Final = {command.name: command for command in COMMANDS}


def looks_like_command(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(COMMAND_PREFIX) and "\n" not in stripped


def parse_command(text: str) -> ParsedCommand | None:
    if not looks_like_command(text):
        return None

    body = text.strip()[len(COMMAND_PREFIX) :]
    name, _, argument = body.partition(" ")
    name = name.strip().casefold()
    if not name or len(name) > MAX_COMMAND_CHARACTERS:
        return None
    if name not in _BY_NAME:
        return None
    return ParsedCommand(name=name, argument=argument.strip())


def unknown_command_message(text: str) -> str:
    body = text.strip()[len(COMMAND_PREFIX) :].split(" ", 1)[0]
    known = ", ".join(command.invocation for command in COMMANDS)
    return f"Unknown command {COMMAND_PREFIX}{body}. Try {known}."


def help_text() -> str:
    widest = max(len(command.invocation) for command in COMMANDS)
    lines = [
        f"{command.invocation.ljust(widest)}  {command.summary}" for command in COMMANDS
    ]
    return "\n".join(lines)
