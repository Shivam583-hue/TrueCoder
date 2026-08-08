"""A server tool must reach the registry without becoming a trusted tool."""

from __future__ import annotations

import json
import unittest

from truecoder.jsonrpc.transport import TransportError
from truecoder.mcp.models import McpToolDescriptor, McpToolResult
from truecoder.mcp.tool import (
    UNTRUSTED_NOTE,
    McpTool,
    namespaced_name,
    tools_for_server,
)
from truecoder.tools import ToolRegistry
from truecoder.tools.base import ToolApproval, ToolArgumentError, ToolExecutionError
from truecoder.tools.builtin import ReadFileTool

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class FakeClient:
    def __init__(
        self, result: McpToolResult | None = None, error: Exception | None = None
    ):
        self.result = result or McpToolResult(text="ok")
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def _descriptor(name: str = "echo", **overrides) -> McpToolDescriptor:
    values = {"name": name, "description": "Echo text.", "schema": SCHEMA}
    values.update(overrides)
    return McpToolDescriptor(**values)  # type: ignore[arg-type]


def _tool(client: FakeClient | None = None, **overrides) -> McpTool:
    return McpTool("files", _descriptor(**overrides), client or FakeClient())  # type: ignore[arg-type]


class NamingTests(unittest.TestCase):
    def test_a_tool_is_namespaced_by_its_server(self):
        self.assertEqual(namespaced_name("files", "echo"), "mcp__files__echo")

    def test_a_server_tool_cannot_shadow_a_builtin(self):
        tool = _tool(name="read_file")

        self.assertNotEqual(tool.name, "read_file")
        self.assertEqual(tool.name, "mcp__files__read_file")

    def test_two_servers_offering_one_name_stay_distinct(self):
        first = McpTool("alpha", _descriptor(), FakeClient())  # type: ignore[arg-type]
        second = McpTool("beta", _descriptor(), FakeClient())  # type: ignore[arg-type]

        self.assertNotEqual(first.name, second.name)

    def test_the_registry_accepts_a_builtin_and_a_server_tool_together(
        self,
    ):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            registry.register(ReadFileTool(Path(directory).resolve()))
            registry.register(_tool(name="read_file"))

            self.assertIn("read_file", registry)
            self.assertIn("mcp__files__read_file", registry)

    def test_an_empty_server_name_is_rejected(self):
        with self.assertRaises(ValueError):
            McpTool("  ", _descriptor(), FakeClient())  # type: ignore[arg-type]

    def test_a_non_descriptor_is_rejected(self):
        with self.assertRaises(TypeError):
            McpTool("files", {"name": "echo"}, FakeClient())  # type: ignore[arg-type]


class DefinitionTests(unittest.TestCase):
    def test_the_server_schema_is_what_the_model_sees(self):
        definition = _tool().definition()

        self.assertEqual(definition.parameters["properties"], SCHEMA["properties"])
        self.assertEqual(definition.parameters["required"], ["text"])

    def test_the_definition_is_not_claimed_to_be_strict(self):
        self.assertFalse(_tool().definition().strict)

    def test_the_description_names_the_server_it_came_from(self):
        self.assertTrue(_tool().description.startswith("[files]"))

    def test_a_tool_without_a_description_still_has_one(self):
        tool = _tool(description="")

        self.assertIn("echo", tool.description)

    def test_approval_is_always_required(self):
        self.assertIs(_tool().approval, ToolApproval.REQUIRED)


class ArgumentTests(unittest.TestCase):
    def test_valid_arguments_are_accepted(self):
        arguments = _tool().parse_arguments(json.dumps({"text": "hi"}))

        self.assertEqual(arguments.model_dump()["text"], "hi")

    def test_a_missing_required_field_is_refused(self):
        with self.assertRaises(ToolArgumentError) as caught:
            _tool().parse_arguments(json.dumps({}))

        self.assertIn("text", str(caught.exception))

    def test_arguments_that_are_not_json_are_refused(self):
        with self.assertRaises(ToolArgumentError):
            _tool().parse_arguments("not json")

    def test_arguments_that_are_not_an_object_are_refused(self):
        with self.assertRaises(ToolArgumentError):
            _tool().parse_arguments("[1, 2]")

    def test_enormous_arguments_are_refused(self):
        with self.assertRaises(ToolArgumentError):
            _tool().parse_arguments(json.dumps({"text": "x" * 200000}))


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_remote_name_is_called_not_the_namespaced_one(self):
        client = FakeClient()
        tool = _tool(client)

        await tool.run(tool.parse_arguments(json.dumps({"text": "hi"})))

        self.assertEqual(client.calls, [("echo", {"text": "hi"})])

    async def test_the_result_is_labelled_as_untrusted(self):
        tool = _tool(FakeClient(McpToolResult(text="some output")))

        output = await tool.run(tool.parse_arguments(json.dumps({"text": "hi"})))

        self.assertEqual(output["content"], "some output")
        self.assertEqual(output["note"], UNTRUSTED_NOTE)
        self.assertEqual(output["server"], "files")

    async def test_a_tool_error_is_reported_as_data(self):
        tool = _tool(FakeClient(McpToolResult(text="refused", is_error=True)))

        output = await tool.run(tool.parse_arguments(json.dumps({"text": "hi"})))

        self.assertEqual(output["status"], "error")
        self.assertEqual(output["content"], "refused")

    async def test_a_truncated_result_says_so(self):
        tool = _tool(FakeClient(McpToolResult(text="partial", truncated=True)))

        output = await tool.run(tool.parse_arguments(json.dumps({"text": "hi"})))

        self.assertTrue(output["truncated"])

    async def test_an_unreachable_server_is_a_domain_error(self):
        tool = _tool(FakeClient(error=TransportError("gone", code="server_exited")))

        with self.assertRaises(ToolExecutionError) as caught:
            await tool.run(tool.parse_arguments(json.dumps({"text": "hi"})))

        self.assertEqual(caught.exception.code, "server_unavailable")


class FactoryTests(unittest.TestCase):
    def test_every_descriptor_becomes_a_namespaced_tool(self):
        client = FakeClient()

        tools = tools_for_server(
            "files",
            (_descriptor("echo"), _descriptor("add")),
            client,  # type: ignore[arg-type]
        )

        self.assertEqual(
            [tool.name for tool in tools],
            ["mcp__files__echo", "mcp__files__add"],
        )

    def test_no_descriptors_means_no_tools(self):
        self.assertEqual(tools_for_server("files", (), FakeClient()), ())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
