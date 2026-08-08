from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from truecoder.mcp.client import McpClient
from truecoder.mcp.configuration import (
    McpConfigError,
    McpServer,
    McpSuite,
    resolve_working_directory,
)
from truecoder.mcp.tool import McpTool, tools_for_server

MAX_REASON_CHARACTERS: Final = 300

ClientFactory = Callable[[McpServer, Path], McpClient]


@dataclass(frozen=True, slots=True)
class ServerStatus:
    name: str
    connected: bool
    tool_count: int = 0
    reason: str | None = None


def default_client_factory(server: McpServer, root: Path) -> McpClient:
    environment = os.environ.copy()
    environment.update(dict(server.environment))
    return McpClient(
        list(server.command),
        cwd=root,
        env=environment,
        request_timeout=server.startup_timeout_seconds,
    )


class McpManager:
    def __init__(
        self,
        suite: McpSuite,
        project_root: Path,
        *,
        client_factory: ClientFactory = default_client_factory,
    ) -> None:
        if not isinstance(suite, McpSuite):
            raise TypeError("suite must be an McpSuite")

        self._suite = suite
        self._project_root = project_root.resolve()
        self._client_factory = client_factory
        self._clients: dict[str, McpClient] = {}
        self._statuses: list[ServerStatus] = []

    @property
    def statuses(self) -> tuple[ServerStatus, ...]:
        return tuple(self._statuses)

    @property
    def unavailable_reason(self) -> str | None:
        return self._suite.unavailable_reason

    async def start(self) -> tuple[McpTool, ...]:
        if self._suite.unavailable_reason is not None:
            return ()

        tools: list[McpTool] = []
        for server in self._suite.servers:
            tools.extend(await self._start_one(server))
        return tuple(tools)

    async def stop(self) -> None:
        for client in list(self._clients.values()):
            await self._discard(client)
        self._clients.clear()

    async def _start_one(self, server: McpServer) -> tuple[McpTool, ...]:
        try:
            root = resolve_working_directory(
                self._project_root,
                server.working_directory,
            )
        except McpConfigError as error:
            self._record(server.name, False, reason=str(error))
            return ()

        client = self._client_factory(server, root)
        try:
            await asyncio.wait_for(
                client.start(),
                timeout=server.startup_timeout_seconds,
            )
            descriptors = await client.list_tools()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one server never breaks startup
            self._record(server.name, False, reason=self._reason(error, client))
            await self._discard(client)
            return ()

        self._clients[server.name] = client
        self._record(server.name, True, tool_count=len(descriptors))
        return tools_for_server(server.name, descriptors, client)

    async def _discard(self, client: McpClient) -> None:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001 - a failed server is already unusable
            return

    def _record(
        self,
        name: str,
        connected: bool,
        *,
        tool_count: int = 0,
        reason: str | None = None,
    ) -> None:
        self._statuses.append(
            ServerStatus(
                name=name,
                connected=connected,
                tool_count=tool_count,
                reason=reason[:MAX_REASON_CHARACTERS] if reason else None,
            )
        )

    @staticmethod
    def _reason(error: Exception, client: McpClient) -> str:
        if isinstance(error, asyncio.TimeoutError):
            return "the server did not finish its handshake in time"
        detail = str(error).strip() or type(error).__name__
        tail = client.stderr_tail.strip()
        return f"{detail} ({tail})" if tail else detail
