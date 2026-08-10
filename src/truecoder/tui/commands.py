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
    SlashCommand("login", "Authorise this provider in your browser"),
    SlashCommand("logout", "Forget the stored authorisation"),
    SlashCommand("help", "List what you can type here"),
    SlashCommand("quit", "Close TrueCoder"),
)

_BY_NAME: Final = {command.name: command for command in COMMANDS}


def looks_like_command(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(COMMAND_PREFIX) and "\n" not in stripped


def command_prefix(text: str) -> str | None:
    if "\n" in text:
        return None

    stripped = text.lstrip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None

    body = stripped[len(COMMAND_PREFIX) :]
    if " " in body or len(body) > MAX_COMMAND_CHARACTERS:
        return None
    return body.casefold()


def matching_commands(text: str) -> tuple[SlashCommand, ...]:
    prefix = command_prefix(text)
    if prefix is None:
        return ()
    return tuple(command for command in COMMANDS if command.name.startswith(prefix))


def _shared_prefix(names: tuple[str, ...]) -> str:
    shortest = min(names, key=len)
    for index, character in enumerate(shortest):
        if any(name[index] != character for name in names):
            return shortest[:index]
    return shortest


def completion(text: str) -> str | None:
    matches = matching_commands(text)
    if not matches:
        return None

    typed = command_prefix(text) or ""
    shared = _shared_prefix(tuple(command.name for command in matches))
    if len(shared) <= len(typed):
        return None
    return f"{COMMAND_PREFIX}{shared}"


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
