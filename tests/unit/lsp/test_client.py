from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.lsp.client import LspClient
from truecoder.lsp.transport import StdioTransport, TransportError

SERVER = Path(__file__).resolve().parents[2] / "helpers" / "lsp_server.py"


class LspClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.source = self.root / "parser.py"
        self.source.write_bytes(b"def parse(raw):\n    return raw\n")
        self.addCleanup(self._directory.cleanup)

    async def _client(self, mode: str | None = None, **kwargs) -> LspClient:
        env = os.environ.copy()
        if mode is not None:
            env["FAKE_LSP_MODE"] = mode
        transport = StdioTransport(
            [sys.executable, str(SERVER)],
            cwd=self.root,
            env=env,
            **kwargs,
        )
        client = LspClient(transport, self.root)
        self.addAsyncCleanup(client.stop)
        await client.start()
        return client

    async def test_starting_completes_the_handshake(self):
        client = await self._client()

        self.assertTrue(client.initialized)
        self.assertTrue(client.supports("definitionProvider"))

    async def test_starting_twice_is_safe(self):
        client = await self._client()

        await client.start()

        self.assertTrue(client.initialized)

    async def test_document_symbols_are_returned(self):
        client = await self._client()

        symbols = await client.document_symbols(self.source)

        self.assertEqual(symbols[0].name, "parse")
        self.assertEqual(symbols[0].kind, "function")

    async def test_workspace_symbols_are_filtered_by_query(self):
        client = await self._client()

        found = await client.workspace_symbols("parse")
        missing = await client.workspace_symbols("nothing")

        self.assertEqual(len(found), 1)
        self.assertEqual(missing, ())

    async def test_definition_returns_a_location(self):
        client = await self._client()

        locations = await client.definition(self.source, 3, 8)

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].range.start.one_based_line, 11)

    async def test_references_return_every_use(self):
        client = await self._client()

        locations = await client.references(self.source, 3, 8)

        self.assertEqual(len(locations), 2)

    async def test_hover_returns_readable_text(self):
        client = await self._client()

        self.assertIn("def parse", await client.hover(self.source, 3, 8))

    async def test_type_definition_and_implementation_are_supported(self):
        client = await self._client()

        self.assertEqual(len(await client.type_definition(self.source, 1, 1)), 1)
        self.assertEqual(len(await client.implementation(self.source, 1, 1)), 1)

    async def test_diagnostics_arrive_after_opening_a_document(self):
        client = await self._client()

        diagnostics = await client.diagnostics(self.source)

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].severity, "error")
        self.assertEqual(diagnostics[0].path, "parser.py")

    async def test_a_document_is_opened_only_once(self):
        client = await self._client()

        first = await client.open_document(self.source)
        second = await client.open_document(self.source)

        self.assertEqual(first, second)

    async def test_an_unreadable_document_is_reported(self):
        client = await self._client()

        with self.assertRaises(TransportError) as caught:
            await client.open_document(self.root / "missing.py")

        self.assertEqual(caught.exception.code, "document_unreadable")

    async def test_closing_a_document_is_safe_when_not_open(self):
        client = await self._client()

        await client.close_document(self.source)

        self.assertTrue(client.initialized)

    async def test_changing_a_document_bumps_its_version(self):
        client = await self._client()
        await client.open_document(self.source)

        await client.change_document(self.source, "def parse(raw):\n    return 1\n")

        self.assertTrue(client.initialized)

    async def test_stopping_leaves_the_client_uninitialized(self):
        client = await self._client()

        await client.stop()

        self.assertFalse(client.initialized)

    async def test_restarting_reestablishes_the_session(self):
        client = await self._client()

        await client.restart()

        self.assertTrue(client.initialized)
        self.assertEqual((await client.document_symbols(self.source))[0].name, "parse")

    async def test_a_server_error_surfaces_to_the_caller(self):
        client = await self._client("error")

        with self.assertRaises(TransportError) as caught:
            await client.definition(self.source, 1, 1)

        self.assertEqual(caught.exception.code, "request_failed")

    async def test_a_transport_is_required(self):
        with self.assertRaises(TypeError):
            LspClient(object(), self.root)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
