from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from truecoder.lsp.transport import StdioTransport, TransportError

SERVER = Path(__file__).resolve().parents[2] / "helpers" / "lsp_server.py"


def _command() -> list[str]:
    return [sys.executable, str(SERVER)]


def _environment(mode: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if mode is not None:
        env["FAKE_LSP_MODE"] = mode
    return env


class StdioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _transport(self, mode: str | None = None, **kwargs) -> StdioTransport:
        transport = StdioTransport(
            _command(),
            cwd=Path.cwd(),
            env=_environment(mode),
            **kwargs,
        )
        self.addAsyncCleanup(transport.stop)
        await transport.start()
        return transport

    async def test_a_started_transport_is_running(self):
        transport = await self._transport()

        self.assertTrue(transport.running)

    async def test_a_request_returns_its_result(self):
        transport = await self._transport()

        result = await transport.request("initialize", {"rootUri": None})

        self.assertIn("capabilities", result)

    async def test_requests_are_correlated_by_id(self):
        transport = await self._transport()
        await transport.request("initialize", {})

        symbols = await transport.request("textDocument/documentSymbol", {})
        hover = await transport.request("textDocument/hover", {})

        self.assertEqual(symbols[0]["name"], "parse")
        self.assertIn("parse", hover["contents"]["value"])

    async def test_a_notification_from_the_server_reaches_the_handler(self):
        received: list[tuple[str, dict]] = []
        transport = await self._transport()
        transport.set_notification_handler(
            lambda method, params: received.append((method, params))
        )
        await transport.request("initialize", {})

        await transport.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": "file:///a.py"}},
        )
        await transport.request("textDocument/hover", {})

        self.assertEqual(received[0][0], "textDocument/publishDiagnostics")
        self.assertEqual(received[0][1]["uri"], "file:///a.py")

    async def test_a_server_error_response_is_reported(self):
        transport = await self._transport("error")
        await transport.request("initialize", {})

        with self.assertRaises(TransportError) as caught:
            await transport.request("textDocument/definition", {})

        self.assertEqual(caught.exception.code, "request_failed")
        self.assertIn("internal failure", caught.exception.message)

    async def test_a_slow_server_times_out(self):
        transport = await self._transport("hang", request_timeout=0.3)
        await transport.request("initialize", {})

        with self.assertRaises(TransportError) as caught:
            await transport.request("textDocument/hover", {})

        self.assertEqual(caught.exception.code, "request_timeout")

    async def test_a_timeout_does_not_break_later_requests(self):
        transport = await self._transport(request_timeout=5.0)
        await transport.request("initialize", {})

        first = await transport.request("textDocument/documentSymbol", {})
        second = await transport.request("textDocument/documentSymbol", {})

        self.assertEqual(first, second)

    async def test_stopping_leaves_the_transport_not_running(self):
        transport = await self._transport()

        await transport.stop()

        self.assertFalse(transport.running)

    async def test_a_request_after_stopping_is_refused(self):
        transport = await self._transport()
        await transport.stop()

        with self.assertRaises(TransportError) as caught:
            await transport.request("initialize", {})

        self.assertEqual(caught.exception.code, "not_running")

    async def test_restarting_yields_a_working_server(self):
        transport = await self._transport()
        await transport.request("initialize", {})

        await transport.restart()

        self.assertTrue(transport.running)
        self.assertIn("capabilities", await transport.request("initialize", {}))

    async def test_stopping_twice_is_safe(self):
        transport = await self._transport()

        await transport.stop()
        await transport.stop()

        self.assertFalse(transport.running)

    async def test_a_server_that_exits_fails_pending_requests(self):
        transport = await self._transport("hang", request_timeout=10.0)
        await transport.request("initialize", {})
        pending = transport.request("textDocument/hover", {})

        await transport.notify("exit")

        with self.assertRaises(TransportError):
            await pending

    async def test_a_server_that_cannot_start_reports_its_stderr(self):
        transport = StdioTransport(
            _command(),
            cwd=Path.cwd(),
            env=_environment("crash_on_start"),
        )
        self.addAsyncCleanup(transport.stop)
        await transport.start()

        with self.assertRaises(TransportError):
            await transport.request("initialize", {}, timeout=5.0)

    async def test_a_missing_executable_is_reported(self):
        transport = StdioTransport(
            ["truecoder-no-such-language-server"],
            cwd=Path.cwd(),
        )

        with self.assertRaises(TransportError) as caught:
            await transport.start()

        self.assertEqual(caught.exception.code, "start_failed")


class TransportConstructionTests(unittest.TestCase):
    def test_an_empty_command_is_rejected(self):
        with self.assertRaises(ValueError):
            StdioTransport([], cwd=Path.cwd())

    def test_an_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            StdioTransport(["x"], cwd=Path.cwd(), request_timeout=0)


if __name__ == "__main__":
    unittest.main()
