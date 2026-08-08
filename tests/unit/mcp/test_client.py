"""The client talks to a real server process and never trusts what it says."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from truecoder.jsonrpc.framing import ProtocolError
from truecoder.jsonrpc.transport import TransportError
from truecoder.mcp.client import McpClient
from truecoder.mcp.models import MAX_RESULT_CHARACTERS
from truecoder.mcp.protocol import PROTOCOL_VERSION
from truecoder.mcp.schema import MAX_DESCRIPTION_LENGTH

SERVER = Path(__file__).resolve().parents[2] / "helpers" / "mcp_server.py"


class McpClientTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, mode: str | None = None) -> McpClient:
        env = os.environ.copy()
        if mode is not None:
            env["FAKE_MCP_MODE"] = mode
        client = McpClient(
            [sys.executable, str(SERVER)],
            cwd=Path.cwd(),
            env=env,
            request_timeout=15.0,
        )
        self.addAsyncCleanup(client.stop)
        await client.start()
        return client

    async def test_the_handshake_records_the_server_protocol_version(self):
        client = await self._client()

        self.assertTrue(client.running)
        self.assertEqual(client.server_protocol_version, PROTOCOL_VERSION)

    async def test_tools_are_listed_with_bounded_schemas(self):
        client = await self._client()

        tools = await client.list_tools()

        names = sorted(tool.name for tool in tools)
        self.assertEqual(names, ["add", "echo"])
        for tool in tools:
            self.assertEqual(tool.schema["type"], "object")
            self.assertFalse(tool.schema["additionalProperties"])

    async def test_a_tool_call_returns_its_text(self):
        client = await self._client()

        result = await client.call_tool("echo", {"text": "hello"})

        self.assertEqual(result.text, "hello")
        self.assertFalse(result.is_error)

    async def test_a_tool_reporting_an_error_is_data_and_not_an_exception(self):
        client = await self._client("tool_error")

        result = await client.call_tool("echo", {"text": "hello"})

        self.assertTrue(result.is_error)
        self.assertIn("refused", result.text)

    async def test_an_unknown_tool_surfaces_the_server_error(self):
        client = await self._client()

        with self.assertRaises(TransportError):
            await client.call_tool("nonexistent", {})

    async def test_a_server_without_a_protocol_version_fails_the_handshake(self):
        env = os.environ.copy()
        env["FAKE_MCP_MODE"] = "no_version"
        client = McpClient(
            [sys.executable, str(SERVER)],
            cwd=Path.cwd(),
            env=env,
            request_timeout=15.0,
        )
        self.addAsyncCleanup(client.stop)

        with self.assertRaises(ProtocolError):
            await client.start()

        self.assertFalse(client.running)

    async def test_calling_before_the_handshake_is_refused(self):
        client = McpClient(
            [sys.executable, str(SERVER)],
            cwd=Path.cwd(),
            request_timeout=15.0,
        )

        with self.assertRaises(TransportError):
            await client.call_tool("echo", {"text": "hi"})

    async def test_a_hostile_listing_keeps_only_what_is_usable(self):
        client = await self._client("hostile_schema")

        tools = await client.list_tools()

        self.assertEqual([tool.name for tool in tools], ["echo"])
        self.assertLessEqual(len(tools[0].description), MAX_DESCRIPTION_LENGTH)
        self.assertFalse(tools[0].schema["additionalProperties"])

    async def test_an_enormous_result_is_bounded(self):
        client = await self._client("huge_tool")

        result = await client.call_tool("huge", {})

        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), MAX_RESULT_CHARACTERS)

    async def test_stopping_twice_is_safe(self):
        client = await self._client()

        await client.stop()
        await client.stop()

        self.assertFalse(client.running)


if __name__ == "__main__":
    unittest.main()
