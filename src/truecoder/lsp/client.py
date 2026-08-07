from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

from truecoder.jsonrpc.transport import StdioTransport, TransportError
from truecoder.lsp.models import (
    Diagnostic,
    Location,
    SymbolInfo,
    display_path,
    language_id_for,
    parse_diagnostics,
    parse_hover,
    parse_locations,
    parse_symbols,
    path_to_uri,
)

MAX_OPEN_DOCUMENTS: Final = 64
SHUTDOWN_TIMEOUT: Final = 3.0

CLIENT_CAPABILITIES: Final[dict[str, Any]] = {
    "workspace": {
        "symbol": {"dynamicRegistration": False},
        "configuration": True,
    },
    "textDocument": {
        "synchronization": {"dynamicRegistration": False, "didSave": False},
        "documentSymbol": {
            "dynamicRegistration": False,
            "hierarchicalDocumentSymbolSupport": True,
        },
        "definition": {"dynamicRegistration": False, "linkSupport": True},
        "typeDefinition": {"dynamicRegistration": False, "linkSupport": True},
        "implementation": {"dynamicRegistration": False, "linkSupport": True},
        "references": {"dynamicRegistration": False},
        "hover": {
            "dynamicRegistration": False,
            "contentFormat": ["markdown", "plaintext"],
        },
        "publishDiagnostics": {"relatedInformation": False},
    },
}


class LspClient:
    def __init__(self, transport: StdioTransport, root: Path) -> None:
        if not isinstance(transport, StdioTransport):
            raise TypeError("transport must be a StdioTransport")

        self._transport = transport
        self._root = root.resolve()
        self._initialized = False
        self._capabilities: dict[str, Any] = {}
        self._open: dict[str, int] = {}
        self._diagnostics: dict[str, tuple[Diagnostic, ...]] = {}
        self._diagnostic_events: dict[str, asyncio.Event] = {}
        self._transport.set_notification_handler(self._on_notification)
        self._transport.set_request_handler(self._on_request)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities)

    def supports(self, capability: str) -> bool:
        return bool(self._capabilities.get(capability))

    async def start(self) -> None:
        if self._initialized:
            return

        await self._transport.start()
        result = await self._transport.request(
            "initialize",
            {
                "processId": None,
                "clientInfo": {"name": "TrueCoder"},
                "rootUri": path_to_uri(self._root),
                "workspaceFolders": self._folders(),
                "capabilities": CLIENT_CAPABILITIES,
            },
        )
        if isinstance(result, dict) and isinstance(result.get("capabilities"), dict):
            self._capabilities = result["capabilities"]

        await self._transport.notify("initialized", {})
        self._initialized = True

    async def stop(self) -> None:
        if self._transport.running and self._initialized:
            try:
                await self._transport.request("shutdown", timeout=SHUTDOWN_TIMEOUT)
                await self._transport.notify("exit")
            except TransportError:
                pass

        self._initialized = False
        self._open.clear()
        self._diagnostics.clear()
        self._diagnostic_events.clear()
        await self._transport.stop()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def open_document(self, path: Path) -> str:
        resolved = path.resolve()
        uri = path_to_uri(resolved)

        if uri in self._open:
            return uri

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise TransportError(
                f"The file could not be read: {error}",
                code="document_unreadable",
            ) from error

        if len(self._open) >= MAX_OPEN_DOCUMENTS:
            await self.close_document(Path(next(iter(self._open))))

        self._open[uri] = 1
        self._diagnostic_events.setdefault(uri, asyncio.Event())
        await self._transport.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id_for(resolved),
                    "version": 1,
                    "text": text,
                }
            },
        )
        return uri

    async def change_document(self, path: Path, text: str) -> None:
        uri = await self.open_document(path)
        version = self._open[uri] + 1
        self._open[uri] = version
        event = self._diagnostic_events.get(uri)
        if event is not None:
            event.clear()
        await self._transport.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )

    async def close_document(self, path: Path) -> None:
        uri = path_to_uri(path.resolve()) if path.is_absolute() else str(path)
        if uri not in self._open:
            return
        del self._open[uri]
        self._diagnostic_events.pop(uri, None)
        await self._transport.notify(
            "textDocument/didClose",
            {"textDocument": {"uri": uri}},
        )

    async def document_symbols(self, path: Path) -> tuple[SymbolInfo, ...]:
        uri = await self.open_document(path)
        result = await self._transport.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
        )
        return parse_symbols(result, self._root, default_uri=uri)

    async def workspace_symbols(self, query: str) -> tuple[SymbolInfo, ...]:
        result = await self._transport.request("workspace/symbol", {"query": query})
        return parse_symbols(result, self._root)

    async def definition(
        self,
        path: Path,
        line: int,
        character: int,
        *,
        method: str = "textDocument/definition",
    ) -> tuple[Location, ...]:
        uri = await self.open_document(path)
        result = await self._transport.request(
            method,
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        return parse_locations(result, self._root)

    async def type_definition(
        self,
        path: Path,
        line: int,
        character: int,
    ) -> tuple[Location, ...]:
        return await self.definition(
            path,
            line,
            character,
            method="textDocument/typeDefinition",
        )

    async def implementation(
        self,
        path: Path,
        line: int,
        character: int,
    ) -> tuple[Location, ...]:
        return await self.definition(
            path,
            line,
            character,
            method="textDocument/implementation",
        )

    async def references(
        self,
        path: Path,
        line: int,
        character: int,
        *,
        include_declaration: bool = False,
    ) -> tuple[Location, ...]:
        uri = await self.open_document(path)
        result = await self._transport.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
        )
        return parse_locations(result, self._root)

    async def hover(self, path: Path, line: int, character: int) -> str:
        uri = await self.open_document(path)
        result = await self._transport.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        return parse_hover(result)

    async def diagnostics(
        self,
        path: Path,
        *,
        wait: float = 2.0,
    ) -> tuple[Diagnostic, ...]:
        uri = await self.open_document(path)
        event = self._diagnostic_events.setdefault(uri, asyncio.Event())

        if not event.is_set() and wait > 0:
            try:
                await asyncio.wait_for(event.wait(), timeout=wait)
            except (TimeoutError, asyncio.TimeoutError):
                pass

        return self._diagnostics.get(uri, ())

    def _folders(self) -> list[dict[str, str]]:
        return [{"uri": path_to_uri(self._root), "name": self._root.name}]

    def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "workspace/workspaceFolders":
            return self._folders()
        if method == "workspace/configuration":
            items = params.get("items")
            count = len(items) if isinstance(items, list) else 1
            return [{} for _ in range(count)]
        return None

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "textDocument/publishDiagnostics":
            return

        uri = params.get("uri")
        if not isinstance(uri, str):
            return

        self._diagnostics[uri] = parse_diagnostics(
            params.get("diagnostics"),
            uri,
            self._root,
        )
        self._diagnostic_events.setdefault(uri, asyncio.Event()).set()

    def workspace_path(self, uri: str) -> str:
        return display_path(uri, self._root)
