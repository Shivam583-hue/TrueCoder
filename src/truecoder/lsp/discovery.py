from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from truecoder.lsp.models import language_id_for

Which = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ServerDefinition:
    name: str
    executable: str
    languages: tuple[str, ...]
    arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A server definition requires a name.")
        if not self.executable.strip():
            raise ValueError("A server definition requires an executable.")
        if not self.languages:
            raise ValueError("A server definition requires at least one language.")


KNOWN_SERVERS: Final[tuple[ServerDefinition, ...]] = (
    ServerDefinition(
        name="pyright",
        executable="pyright-langserver",
        languages=("python",),
        arguments=("--stdio",),
    ),
    ServerDefinition(
        name="pylsp",
        executable="pylsp",
        languages=("python",),
    ),
    ServerDefinition(
        name="jedi",
        executable="jedi-language-server",
        languages=("python",),
    ),
    ServerDefinition(
        name="typescript",
        executable="typescript-language-server",
        languages=("typescript", "typescriptreact", "javascript", "javascriptreact"),
        arguments=("--stdio",),
    ),
    ServerDefinition(
        name="rust-analyzer",
        executable="rust-analyzer",
        languages=("rust",),
    ),
    ServerDefinition(
        name="gopls",
        executable="gopls",
        languages=("go",),
    ),
    ServerDefinition(
        name="clangd",
        executable="clangd",
        languages=("c", "cpp"),
    ),
)


@dataclass(frozen=True, slots=True)
class DiscoveredServer:
    definition: ServerDefinition
    path: str

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def languages(self) -> tuple[str, ...]:
        return self.definition.languages

    @property
    def command(self) -> tuple[str, ...]:
        return (self.path, *self.definition.arguments)


def discover_servers(
    definitions: Sequence[ServerDefinition] = KNOWN_SERVERS,
    *,
    which: Which = shutil.which,
) -> tuple[DiscoveredServer, ...]:
    discovered: list[DiscoveredServer] = []
    for definition in definitions:
        located = which(definition.executable)
        if located:
            discovered.append(DiscoveredServer(definition=definition, path=located))
    return tuple(discovered)


def server_for_language(
    language: str,
    servers: Sequence[DiscoveredServer],
) -> DiscoveredServer | None:
    for server in servers:
        if language in server.languages:
            return server
    return None


def server_for_path(
    path: Path,
    servers: Sequence[DiscoveredServer],
) -> DiscoveredServer | None:
    return server_for_language(language_id_for(path), servers)


def supported_languages(servers: Sequence[DiscoveredServer]) -> tuple[str, ...]:
    languages: list[str] = []
    for server in servers:
        for language in server.languages:
            if language not in languages:
                languages.append(language)
    return tuple(languages)
