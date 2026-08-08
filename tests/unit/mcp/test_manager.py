"""One broken server must never stop the others, or the application, from starting."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.mcp.client import McpClient
from truecoder.mcp.configuration import McpServer, McpSuite
from truecoder.mcp.manager import McpManager

SERVER = Path(__file__).resolve().parents[2] / "helpers" / "mcp_server.py"


def _server(name: str = "files", **overrides) -> McpServer:
    values = {"name": name, "command": (sys.executable, str(SERVER))}
    values.update(overrides)
    return McpServer(**values)  # type: ignore[arg-type]


def _factory(mode: str | None = None):
    def build(server: McpServer, root: Path) -> McpClient:
        environment = os.environ.copy()
        if mode is not None:
            environment["FAKE_MCP_MODE"] = mode
        return McpClient(
            list(server.command),
            cwd=root,
            env=environment,
            request_timeout=15.0,
        )

    return build


class McpManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    async def _manager(self, suite: McpSuite, mode: str | None = None) -> McpManager:
        manager = McpManager(suite, self.root, client_factory=_factory(mode))
        self.addAsyncCleanup(manager.stop)
        return manager

    async def test_a_healthy_server_contributes_namespaced_tools(self):
        manager = await self._manager(McpSuite(servers=(_server(),)))

        tools = await manager.start()

        self.assertEqual(
            sorted(tool.name for tool in tools),
            ["mcp__files__add", "mcp__files__echo"],
        )
        self.assertEqual(manager.statuses[0].connected, True)
        self.assertEqual(manager.statuses[0].tool_count, 2)

    async def test_a_broken_server_is_reported_and_never_raised(self):
        suite = McpSuite(servers=(_server(command=("truecoder-no-such-server",)),))
        manager = await self._manager(suite)

        tools = await manager.start()

        self.assertEqual(tools, ())
        self.assertFalse(manager.statuses[0].connected)
        self.assertIsNotNone(manager.statuses[0].reason)

    async def test_a_broken_server_does_not_stop_a_healthy_one(self):
        suite = McpSuite(
            servers=(
                _server("broken", command=("truecoder-no-such-server",)),
                _server("files"),
            )
        )
        manager = await self._manager(suite)

        tools = await manager.start()

        self.assertTrue(all(tool.server == "files" for tool in tools))
        self.assertEqual(len(tools), 2)
        self.assertEqual(
            [status.connected for status in manager.statuses], [False, True]
        )

    async def test_an_unavailable_suite_starts_nothing(self):
        suite = McpSuite(unavailable_reason="broken configuration")
        manager = await self._manager(suite)

        tools = await manager.start()

        self.assertEqual(tools, ())
        self.assertEqual(manager.unavailable_reason, "broken configuration")

    async def test_an_escaping_working_directory_is_refused(self):
        suite = McpSuite(servers=(_server(working_directory="../outside"),))
        manager = await self._manager(suite)

        tools = await manager.start()

        self.assertEqual(tools, ())
        self.assertIn("workspace", manager.statuses[0].reason or "")

    async def test_a_server_that_never_completes_the_handshake_times_out(self):
        suite = McpSuite(
            servers=(
                _server(
                    command=(sys.executable, "-c", "import time; time.sleep(30)"),
                    startup_timeout_seconds=0.5,
                ),
            )
        )
        manager = McpManager(suite, self.root)
        self.addAsyncCleanup(manager.stop)

        tools = await manager.start()

        self.assertEqual(tools, ())
        self.assertIn("time", manager.statuses[0].reason or "")

    async def test_stopping_without_starting_is_safe(self):
        manager = McpManager(McpSuite(servers=(_server(),)), self.root)

        await manager.stop()

        self.assertEqual(manager.statuses, ())

    async def test_a_non_suite_is_rejected(self):
        with self.assertRaises(TypeError):
            McpManager(object(), self.root)  # type: ignore[arg-type]

    async def test_tools_can_actually_be_called_through_the_manager(self):
        manager = await self._manager(McpSuite(servers=(_server(),)))
        tools = await manager.start()
        echo = next(tool for tool in tools if tool.remote_name == "echo")

        output = await echo.run(echo.parse_arguments('{"text": "hello"}'))

        self.assertEqual(output["content"], "hello")
        self.assertEqual(output["status"], "success")


if __name__ == "__main__":
    unittest.main()
