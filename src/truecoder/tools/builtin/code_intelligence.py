from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from pydantic import Field

from truecoder.lsp.manager import LspManager, LspUnavailableError
from truecoder.lsp.models import Diagnostic, Location, SymbolInfo
from truecoder.lsp.transport import TransportError
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.builtin.filesystem import resolve_existing_workspace_path
from truecoder.tools.context import ToolInvocationContext

MAX_RESULTS = 200


class SymbolHit(TypedDict):
    name: str
    kind: str
    container: str
    path: str
    line: int


class LocationHit(TypedDict):
    path: str
    line: int
    column: int


class DiagnosticHit(TypedDict):
    severity: str
    message: str
    path: str
    line: int
    source: str


def _symbol_hits(symbols: tuple[SymbolInfo, ...]) -> list[SymbolHit]:
    return [
        {
            "name": symbol.name,
            "kind": symbol.kind,
            "container": symbol.container,
            "path": symbol.location.path,
            "line": symbol.location.range.start.one_based_line,
        }
        for symbol in symbols[:MAX_RESULTS]
    ]


def _location_hits(locations: tuple[Location, ...]) -> list[LocationHit]:
    return [
        {
            "path": location.path,
            "line": location.range.start.one_based_line,
            "column": location.range.start.character + 1,
        }
        for location in locations[:MAX_RESULTS]
    ]


def _diagnostic_hits(diagnostics: tuple[Diagnostic, ...]) -> list[DiagnosticHit]:
    return [
        {
            "severity": diagnostic.severity,
            "message": diagnostic.message,
            "path": diagnostic.path,
            "line": diagnostic.range.start.one_based_line,
            "source": diagnostic.source,
        }
        for diagnostic in diagnostics[:MAX_RESULTS]
    ]


class _CodeIntelligenceTool(BaseTool[Any]):
    approval = ToolApproval.REQUIRED

    def __init__(self, manager: LspManager) -> None:
        if not isinstance(manager, LspManager):
            raise TypeError("manager must be an LspManager.")

        self._manager = manager

    @property
    def manager(self) -> LspManager:
        return self._manager

    async def aclose(self) -> None:
        await self._manager.stop_all()

    def _resolve(self, path: str) -> Path:
        return resolve_existing_workspace_path(
            self._manager.root,
            path,
            expected="file",
        )

    async def _client(self, path: Path):
        try:
            return await self._manager.client_for(path)
        except LspUnavailableError as error:
            raise ToolExecutionError(error.message, code=error.code) from error

    @staticmethod
    def _fail(error: TransportError) -> ToolExecutionError:
        return ToolExecutionError(error.message, code=error.code)


class FindSymbolArguments(ToolArguments):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Symbol name or fragment to search for across the workspace.",
    )


class FindSymbolOutput(TypedDict):
    query: str
    symbols: list[SymbolHit]
    count: int
    truncated: bool


class FindSymbolTool(_CodeIntelligenceTool):
    name = "find_symbol"
    description = (
        "Find where a symbol is declared anywhere in the workspace, using the "
        "language server rather than text search. Returns the kind, containing "
        "scope, file, and line for each declaration."
    )
    arguments_type = FindSymbolArguments

    async def run(
        self,
        arguments: FindSymbolArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> FindSymbolOutput:
        del invocation

        anchor = next(
            (
                path
                for path in sorted(self._manager.root.rglob("*"))
                if path.is_file() and self._manager.server_for(path) is not None
            ),
            None,
        )
        if anchor is None:
            raise ToolExecutionError(
                "No file in this workspace matches an available language server.",
                code="no_server",
            )

        client = await self._client(anchor)
        try:
            symbols = await client.workspace_symbols(arguments.query)
        except TransportError as error:
            raise self._fail(error) from error

        return {
            "query": arguments.query,
            "symbols": _symbol_hits(symbols),
            "count": len(symbols),
            "truncated": len(symbols) > MAX_RESULTS,
        }


class PositionArguments(ToolArguments):
    path: str = Field(
        min_length=1,
        description="File path relative to the workspace.",
    )
    line: int = Field(
        ge=1,
        description="One-based line number of the symbol.",
    )
    column: int = Field(
        ge=1,
        description="One-based column number of the symbol on that line.",
    )


class GotoDefinitionOutput(TypedDict):
    path: str
    line: int
    column: int
    definitions: list[LocationHit]


class GotoDefinitionTool(_CodeIntelligenceTool):
    name = "goto_definition"
    description = (
        "Resolve the symbol at a file position to where it is defined. Use it "
        "instead of guessing which definition a name refers to."
    )
    arguments_type = PositionArguments

    async def run(
        self,
        arguments: PositionArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> GotoDefinitionOutput:
        del invocation
        target = self._resolve(arguments.path)
        client = await self._client(target)

        try:
            locations = await client.definition(
                target,
                arguments.line - 1,
                arguments.column - 1,
            )
        except TransportError as error:
            raise self._fail(error) from error

        return {
            "path": arguments.path,
            "line": arguments.line,
            "column": arguments.column,
            "definitions": _location_hits(locations),
        }


class FindReferencesOutput(TypedDict):
    path: str
    line: int
    column: int
    references: list[LocationHit]
    count: int
    truncated: bool


class FindReferencesTool(_CodeIntelligenceTool):
    name = "find_references"
    description = (
        "List every place the symbol at a file position is used, resolved by "
        "the language server rather than by text match."
    )
    arguments_type = PositionArguments

    async def run(
        self,
        arguments: PositionArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> FindReferencesOutput:
        del invocation
        target = self._resolve(arguments.path)
        client = await self._client(target)

        try:
            locations = await client.references(
                target,
                arguments.line - 1,
                arguments.column - 1,
                include_declaration=True,
            )
        except TransportError as error:
            raise self._fail(error) from error

        return {
            "path": arguments.path,
            "line": arguments.line,
            "column": arguments.column,
            "references": _location_hits(locations),
            "count": len(locations),
            "truncated": len(locations) > MAX_RESULTS,
        }


class GetDiagnosticsArguments(ToolArguments):
    path: str = Field(
        min_length=1,
        description="File path relative to the workspace.",
    )


class GetDiagnosticsOutput(TypedDict):
    path: str
    diagnostics: list[DiagnosticHit]
    count: int
    truncated: bool


class GetDiagnosticsTool(_CodeIntelligenceTool):
    name = "get_diagnostics"
    description = (
        "Report the errors and warnings the language server sees in one file. "
        "Use it to check work without running a build."
    )
    arguments_type = GetDiagnosticsArguments

    async def run(
        self,
        arguments: GetDiagnosticsArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> GetDiagnosticsOutput:
        del invocation
        target = self._resolve(arguments.path)
        client = await self._client(target)

        try:
            diagnostics = await client.diagnostics(target)
        except TransportError as error:
            raise self._fail(error) from error

        return {
            "path": arguments.path,
            "diagnostics": _diagnostic_hits(diagnostics),
            "count": len(diagnostics),
            "truncated": len(diagnostics) > MAX_RESULTS,
        }


def code_intelligence_tools(manager: LspManager) -> tuple[BaseTool[Any], ...]:
    return (
        FindSymbolTool(manager),
        FindReferencesTool(manager),
        GetDiagnosticsTool(manager),
        GotoDefinitionTool(manager),
    )
