from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path

from truecoder.lsp.client import LspClient
from truecoder.lsp.discovery import (
    DiscoveredServer,
    discover_servers,
    server_for_path,
    supported_languages,
)
from truecoder.lsp.models import language_id_for
from truecoder.lsp.transport import StdioTransport, TransportError

TransportFactory = Callable[[DiscoveredServer, Path], StdioTransport]


def default_transport_factory(
    server: DiscoveredServer,
    root: Path,
) -> StdioTransport:
    return StdioTransport(server.command, cwd=root)


class LspUnavailableError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class LspManager:
    def __init__(
        self,
        root: Path,
        *,
        servers: Sequence[DiscoveredServer] | None = None,
        transport_factory: TransportFactory = default_transport_factory,
    ) -> None:
        self._root = root.resolve()
        self._servers = tuple(servers) if servers is not None else discover_servers()
        self._transport_factory = transport_factory
        self._clients: dict[str, LspClient] = {}
        self._failed: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def servers(self) -> tuple[DiscoveredServer, ...]:
        return self._servers

    @property
    def languages(self) -> tuple[str, ...]:
        return supported_languages(self._servers)

    @property
    def running(self) -> tuple[str, ...]:
        return tuple(sorted(self._clients))

    def server_for(self, path: Path) -> DiscoveredServer | None:
        return server_for_path(path, self._servers)

    async def client_for(self, path: Path) -> LspClient:
        server = self.server_for(path)
        if server is None:
            raise LspUnavailableError(
                f"No language server is available for {language_id_for(path)} files. "
                f"Available: {', '.join(self.languages) or 'none'}.",
                code="no_server",
            )

        async with self._lock:
            existing = self._clients.get(server.name)
            if existing is not None:
                return existing

            previous = self._failed.get(server.name)
            if previous is not None:
                raise LspUnavailableError(previous, code="server_unavailable")

            transport = self._transport_factory(server, self._root)
            client = LspClient(transport, self._root)
            try:
                await client.start()
            except (TransportError, OSError) as error:
                message = f"The {server.name} language server could not start: {error}"
                self._failed[server.name] = message
                await client.stop()
                raise LspUnavailableError(message, code="server_unavailable") from error

            self._clients[server.name] = client
            return client

    async def restart(self, name: str) -> None:
        client = self._clients.get(name)
        self._failed.pop(name, None)
        if client is not None:
            await client.restart()

    async def stop_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        await asyncio.gather(
            *(client.stop() for client in clients),
            return_exceptions=True,
        )
