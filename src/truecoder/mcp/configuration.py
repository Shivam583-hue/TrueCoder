from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.mcp.models import usable_tool_name

MCP_CONFIG_VERSION: Final = 1
MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_SERVERS: Final = 16
MAX_COMMAND_PARTS: Final = 32
MAX_ENVIRONMENT_ENTRIES: Final = 32
DEFAULT_STARTUP_TIMEOUT: Final = 30.0

_SERVER_FIELDS: Final = frozenset(
    {"name", "command", "environment", "working_directory", "startup_timeout_seconds"}
)
_ROOT_FIELDS: Final = frozenset({"version", "servers"})


class McpConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class McpServer:
    name: str
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    working_directory: str = "."
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT

    def __post_init__(self) -> None:
        if not usable_tool_name(self.name):
            raise McpConfigError(f"unusable server name: {self.name!r}")
        if not self.command:
            raise McpConfigError(f"server {self.name!r} has no command")
        if len(self.command) > MAX_COMMAND_PARTS:
            raise McpConfigError(f"server {self.name!r} has too many command parts")
        for part in self.command:
            if not isinstance(part, str) or not part.strip():
                raise McpConfigError(f"server {self.name!r} has an empty command part")
        if self.startup_timeout_seconds <= 0:
            raise McpConfigError(
                f"server {self.name!r} needs a positive startup timeout"
            )


@dataclass(frozen=True, slots=True)
class McpSuite:
    servers: tuple[McpServer, ...] = ()
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and bool(self.servers)


def default_mcp_config_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "mcp.json"


def load_mcp_servers(path: Path | None = None) -> McpSuite:
    target = path or default_mcp_config_path()
    if not isinstance(target, Path):
        raise McpConfigError("path must be a pathlib.Path")

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return McpSuite()
    except (OSError, UnicodeDecodeError) as error:
        return McpSuite(
            unavailable_reason=f"mcp configuration could not be read: {error}"
        )

    try:
        return parse_mcp_servers(raw)
    except McpConfigError as error:
        return McpSuite(unavailable_reason=str(error))


def parse_mcp_servers(raw: str) -> McpSuite:
    if not isinstance(raw, str):
        raise McpConfigError("mcp configuration must be text")
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise McpConfigError("mcp configuration is too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise McpConfigError(f"mcp configuration is not valid JSON: {error}") from None

    if not isinstance(payload, dict):
        raise McpConfigError("mcp configuration must be a JSON object")

    unknown = set(payload) - _ROOT_FIELDS
    if unknown:
        raise McpConfigError(f"unknown configuration field(s): {sorted(unknown)}")

    if payload.get("version") != MCP_CONFIG_VERSION:
        raise McpConfigError(f"mcp configuration version must be {MCP_CONFIG_VERSION}")

    listed = payload.get("servers", [])
    if not isinstance(listed, list):
        raise McpConfigError("servers must be a list")
    if len(listed) > MAX_SERVERS:
        raise McpConfigError(f"at most {MAX_SERVERS} servers are supported")

    servers = tuple(_server(entry) for entry in listed)
    names = [server.name for server in servers]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise McpConfigError(f"duplicate server name(s): {duplicates}")

    return McpSuite(servers=servers)


def _server(entry: object) -> McpServer:
    if not isinstance(entry, dict):
        raise McpConfigError("each server must be a JSON object")

    unknown = set(entry) - _SERVER_FIELDS
    if unknown:
        raise McpConfigError(f"unknown server field(s): {sorted(unknown)}")

    name = entry.get("name")
    if not isinstance(name, str):
        raise McpConfigError("each server needs a name")

    command = entry.get("command")
    if not isinstance(command, list):
        raise McpConfigError(f"server {name!r} needs a command list")

    return McpServer(
        name=name,
        command=tuple(str(part) for part in command),
        environment=_environment(name, entry.get("environment")),
        working_directory=_working_directory(name, entry.get("working_directory")),
        startup_timeout_seconds=_timeout(name, entry.get("startup_timeout_seconds")),
    )


def _environment(name: str, value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise McpConfigError(f"server {name!r} environment must be an object")
    if len(value) > MAX_ENVIRONMENT_ENTRIES:
        raise McpConfigError(f"server {name!r} declares too many environment entries")

    entries: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise McpConfigError(f"server {name!r} has an unusable environment name")
        if not isinstance(item, str):
            raise McpConfigError(
                f"server {name!r} environment value for {key!r} must be text"
            )
        entries.append((key, item))
    return tuple(entries)


def _working_directory(name: str, value: object) -> str:
    if value is None:
        return "."
    if not isinstance(value, str) or not value.strip():
        raise McpConfigError(f"server {name!r} working directory must be text")
    if Path(value).is_absolute():
        raise McpConfigError(
            f"server {name!r} working directory must be workspace-relative"
        )
    return value


def _timeout(name: str, value: object) -> float:
    if value is None:
        return DEFAULT_STARTUP_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McpConfigError(f"server {name!r} startup timeout must be a number")
    return float(value)


def resolve_working_directory(project_root: Path, requested: str) -> Path:
    candidate = Path(requested)
    if candidate.is_absolute():
        raise McpConfigError("a server working directory must be workspace-relative")

    resolved = (project_root / candidate).resolve()
    root = project_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise McpConfigError(
            "a server working directory must stay inside the workspace"
        )
    return resolved
