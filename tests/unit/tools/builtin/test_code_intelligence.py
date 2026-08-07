from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.jsonrpc.transport import StdioTransport
from truecoder.lsp.discovery import DiscoveredServer, ServerDefinition
from truecoder.lsp.manager import LspManager
from truecoder.lsp.protocol import HeaderFraming
from truecoder.tools.base import (
    ToolApproval,
    ToolArgumentError,
    ToolCall,
    ToolResultStatus,
)
from truecoder.tools.builtin.code_intelligence import (
    FindReferencesTool,
    FindSymbolArguments,
    FindSymbolTool,
    GetDiagnosticsArguments,
    GetDiagnosticsTool,
    GotoDefinitionTool,
    PositionArguments,
    code_intelligence_tools,
)
from truecoder.tools.executor import ToolExecutor
from truecoder.tools.registry import ToolRegistry

SERVER = Path(__file__).resolve().parents[3] / "helpers" / "lsp_server.py"

PYTHON = ServerDefinition(
    name="fake-python",
    executable="fake-python-server",
    languages=("python",),
)


class CodeIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        (self.root / "parser.py").write_bytes(b"def parse(raw):\n    return raw\n")
        self.addCleanup(self._directory.cleanup)

    def _manager(self, *, mode: str | None = None, servers=None) -> LspManager:
        env = os.environ.copy()
        if mode is not None:
            env["FAKE_LSP_MODE"] = mode

        def factory(server, root):
            del server
            return StdioTransport(
                [sys.executable, str(SERVER)],
                framing=HeaderFraming(),
                cwd=root,
                env=env,
                request_timeout=5.0,
            )

        manager = LspManager(
            self.root,
            servers=(DiscoveredServer(definition=PYTHON, path="/bin/py"),)
            if servers is None
            else servers,
            transport_factory=factory,
        )
        self.addAsyncCleanup(manager.stop_all)
        return manager

    def test_every_tool_requires_approval(self):
        for tool in code_intelligence_tools(self._manager()):
            with self.subTest(tool=tool.name):
                self.assertIs(tool.approval, ToolApproval.REQUIRED)

    def test_the_expected_tools_are_provided(self):
        names = sorted(tool.name for tool in code_intelligence_tools(self._manager()))

        self.assertEqual(
            names,
            ["find_references", "find_symbol", "get_diagnostics", "goto_definition"],
        )

    def test_a_manager_is_required(self):
        with self.assertRaises(TypeError):
            FindSymbolTool(object())  # type: ignore[arg-type]

    async def test_find_symbol_returns_workspace_declarations(self):
        tool = FindSymbolTool(self._manager())

        output = await tool.run(FindSymbolArguments(query="parse"))

        self.assertEqual(output["count"], 1)
        self.assertEqual(output["symbols"][0]["name"], "parse")
        self.assertEqual(output["symbols"][0]["kind"], "function")
        self.assertFalse(output["truncated"])

    async def test_find_symbol_reports_an_empty_search(self):
        tool = FindSymbolTool(self._manager())

        output = await tool.run(FindSymbolArguments(query="absent"))

        self.assertEqual(output["symbols"], [])
        self.assertEqual(output["count"], 0)

    async def test_goto_definition_converts_to_one_based_lines(self):
        tool = GotoDefinitionTool(self._manager())

        output = await tool.run(
            PositionArguments(path="parser.py", line=4, column=9)
        )

        self.assertEqual(output["definitions"][0]["line"], 11)
        self.assertEqual(output["definitions"][0]["column"], 5)
        self.assertEqual(output["definitions"][0]["path"], "/workspace/parser.py")

    async def test_a_result_inside_the_workspace_is_shown_relative(self):
        tool = GetDiagnosticsTool(self._manager())

        output = await tool.run(GetDiagnosticsArguments(path="parser.py"))

        self.assertEqual(output["diagnostics"][0]["path"], "parser.py")

    async def test_find_references_lists_every_use(self):
        tool = FindReferencesTool(self._manager())

        output = await tool.run(
            PositionArguments(path="parser.py", line=4, column=9)
        )

        self.assertEqual(output["count"], 2)
        self.assertEqual(len(output["references"]), 2)

    async def test_get_diagnostics_reports_severity_and_line(self):
        tool = GetDiagnosticsTool(self._manager())

        output = await tool.run(GetDiagnosticsArguments(path="parser.py"))

        self.assertEqual(output["count"], 1)
        self.assertEqual(output["diagnostics"][0]["severity"], "error")
        self.assertEqual(output["diagnostics"][0]["line"], 3)
        self.assertIn("undefined name", output["diagnostics"][0]["message"])

    async def test_a_path_outside_the_workspace_is_refused(self):
        tool = GetDiagnosticsTool(self._manager())
        registry = ToolRegistry()
        registry.register(tool)
        call = ToolCall(
            "call_1",
            "get_diagnostics",
            json.dumps({"path": "../escape.py"}),
        )

        result = await ToolExecutor(registry).execute(call, approved=True)

        self.assertIs(result.status, ToolResultStatus.ERROR)

    async def test_a_language_without_a_server_is_reported(self):
        (self.root / "notes.zzz").write_bytes(b"text\n")
        tool = GetDiagnosticsTool(self._manager())
        registry = ToolRegistry()
        registry.register(tool)
        call = ToolCall(
            "call_1",
            "get_diagnostics",
            json.dumps({"path": "notes.zzz"}),
        )

        result = await ToolExecutor(registry).execute(call, approved=True)

        self.assertIs(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "no_server")

    async def test_a_server_failure_is_a_recoverable_tool_error(self):
        tool = GotoDefinitionTool(self._manager(mode="error"))
        registry = ToolRegistry()
        registry.register(tool)
        call = ToolCall(
            "call_1",
            "goto_definition",
            json.dumps({"path": "parser.py", "line": 1, "column": 1}),
        )

        result = await ToolExecutor(registry).execute(call, approved=True)

        self.assertIs(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "request_failed")

    async def test_find_symbol_without_any_matching_file_is_reported(self):
        tool = FindSymbolTool(self._manager(servers=()))
        registry = ToolRegistry()
        registry.register(tool)
        call = ToolCall("call_1", "find_symbol", json.dumps({"query": "x"}))

        result = await ToolExecutor(registry).execute(call, approved=True)

        self.assertEqual(result.error_code, "no_server")

    async def test_the_tools_share_one_server_process(self):
        manager = self._manager()
        tools = code_intelligence_tools(manager)

        await tools[0].run(FindSymbolArguments(query="parse"))
        await tools[2].run(GetDiagnosticsArguments(path="parser.py"))

        self.assertEqual(manager.running, ("fake-python",))

    def test_line_and_column_must_be_one_based(self):
        tool = GotoDefinitionTool(self._manager())

        with self.assertRaises(ToolArgumentError):
            tool.parse_arguments(
                json.dumps({"path": "parser.py", "line": 0, "column": 1})
            )
        with self.assertRaises(ToolArgumentError):
            tool.parse_arguments(
                json.dumps({"path": "parser.py", "line": 1, "column": 0})
            )

    def test_unknown_fields_are_refused(self):
        tool = GetDiagnosticsTool(self._manager())

        with self.assertRaises(ToolArgumentError):
            tool.parse_arguments(json.dumps({"path": "a.py", "deep": True}))


if __name__ == "__main__":
    unittest.main()
