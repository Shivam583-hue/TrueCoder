from __future__ import annotations

from dataclasses import dataclass
from typing import Final

COMMAND_PREFIX: Final = "/"
MAX_COMMAND_CHARACTERS: Final = 64


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    summary: str
    aliases: tuple[str, ...] = ()
    visible: bool = True

    def __post_init__(self) -> None:
        for spelling in self.names:
            if not spelling.strip() or " " in spelling:
                raise ValueError("a command name must be one word")
        if len(set(self.names)) != len(self.names):
            raise ValueError(f"command {self.name!r} repeats a spelling")
        if not self.summary.strip():
            raise ValueError(f"command {self.name!r} needs a summary")

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def invocation(self) -> str:
        return f"{COMMAND_PREFIX}{self.name}"


@dataclass(frozen=True, slots=True)
class CommandMatch:
    command: SlashCommand
    spelling: str

    @property
    def invocation(self) -> str:
        return f"{COMMAND_PREFIX}{self.spelling}"

    @property
    def summary(self) -> str:
        return self.command.summary


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    argument: str


COMMANDS: Final = (
    SlashCommand("models", "Choose a provider and model"),
    SlashCommand("effort", "Set how deeply the model reasons"),
    SlashCommand("model", "Show the model currently answering", visible=False),
    SlashCommand("connect", "Connect an AI provider", visible=False),
    SlashCommand("login", "Reconnect the current provider"),
    SlashCommand("logout", "Forget the stored authorisation"),
    SlashCommand("help", "List what you can type here"),
    SlashCommand("quit", "Close TrueCoder", aliases=("exit",)),
)

_BY_NAME: Final = {
    spelling: command for command in COMMANDS for spelling in command.names
}

ALL_MATCHES: Final = tuple(
    CommandMatch(command, spelling)
    for command in COMMANDS
    if command.visible
    for spelling in command.names
)
SPELLINGS: Final = tuple(match.spelling for match in ALL_MATCHES)


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


def matching_commands(text: str) -> tuple[CommandMatch, ...]:
    prefix = command_prefix(text)
    if prefix is None:
        return ()
    return tuple(match for match in ALL_MATCHES if match.spelling.startswith(prefix))


def _shared_prefix(spellings: tuple[str, ...]) -> str:
    shortest = min(spellings, key=len)
    for index, character in enumerate(shortest):
        if any(spelling[index] != character for spelling in spellings):
            return shortest[:index]
    return shortest


def completion(text: str) -> str | None:
    matches = matching_commands(text)
    if not matches:
        return None

    typed = command_prefix(text) or ""
    shared = _shared_prefix(tuple(match.spelling for match in matches))
    if len(shared) <= len(typed):
        return None
    return f"{COMMAND_PREFIX}{shared}"


def parse_command(text: str) -> ParsedCommand | None:
    if not looks_like_command(text):
        return None

    body = text.strip()[len(COMMAND_PREFIX) :]
    spelling, _, argument = body.partition(" ")
    spelling = spelling.strip().casefold()
    if not spelling or len(spelling) > MAX_COMMAND_CHARACTERS:
        return None

    command = _BY_NAME.get(spelling)
    if command is None:
        return None
    return ParsedCommand(name=command.name, argument=argument.strip())


def unknown_command_message(text: str) -> str:
    body = text.strip()[len(COMMAND_PREFIX) :].split(" ", 1)[0]
    known = ", ".join(
        command.invocation for command in COMMANDS if command.visible
    )
    return f"Unknown command {COMMAND_PREFIX}{body}. Try {known}."


def help_text() -> str:
    widest = max(len(match.invocation) for match in ALL_MATCHES)
    lines = [
        f"{match.invocation.ljust(widest)}  {match.summary}" for match in ALL_MATCHES
    ]
    return "\n".join(lines)
