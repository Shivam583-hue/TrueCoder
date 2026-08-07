from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.lsp.discovery import (
    KNOWN_SERVERS,
    DiscoveredServer,
    ServerDefinition,
    discover_servers,
    server_for_language,
    server_for_path,
    supported_languages,
)
from truecoder.lsp.manager import LspManager, LspUnavailableError
from truecoder.lsp.transport import StdioTransport

SERVER = Path(__file__).resolve().parents[2] / "helpers" / "lsp_server.py"

PYTHON = ServerDefinition(
    name="fake-python",
    executable="fake-python-server",
    languages=("python",),
    arguments=("--stdio",),
)
RUST = ServerDefinition(
    name="fake-rust",
    executable="fake-rust-server",
    languages=("rust",),
)


class ServerDefinitionTests(unittest.TestCase):
    def test_a_definition_requires_a_name(self):
        with self.assertRaises(ValueError):
            ServerDefinition(name=" ", executable="x", languages=("python",))

    def test_a_definition_requires_an_executable(self):
        with self.assertRaises(ValueError):
            ServerDefinition(name="x", executable=" ", languages=("python",))

    def test_a_definition_requires_a_language(self):
        with self.assertRaises(ValueError):
            ServerDefinition(name="x", executable="y", languages=())

    def test_the_shipped_definitions_are_valid(self):
        self.assertTrue(KNOWN_SERVERS)
        self.assertIn("python", supported_languages(
            [DiscoveredServer(definition=d, path="/x") for d in KNOWN_SERVERS]
        ))


class DiscoverServersTests(unittest.TestCase):
    def test_only_servers_on_the_path_are_discovered(self):
        def which(executable: str) -> str | None:
            return "/usr/bin/fake-python-server" if executable == PYTHON.executable else None

        discovered = discover_servers([PYTHON, RUST], which=which)

        self.assertEqual([server.name for server in discovered], ["fake-python"])
        self.assertEqual(discovered[0].path, "/usr/bin/fake-python-server")

    def test_nothing_is_discovered_when_nothing_is_installed(self):
        self.assertEqual(discover_servers([PYTHON], which=lambda _: None), ())

    def test_a_discovered_command_includes_its_arguments(self):
        discovered = discover_servers([PYTHON], which=lambda _: "/bin/x")

        self.assertEqual(discovered[0].command, ("/bin/x", "--stdio"))

    def test_the_real_path_is_searched_without_error(self):
        discover_servers()


class ServerSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.servers = (
            DiscoveredServer(definition=PYTHON, path="/bin/py"),
            DiscoveredServer(definition=RUST, path="/bin/rs"),
        )

    def test_a_language_selects_its_server(self):
        selected = server_for_language("rust", self.servers)

        assert selected is not None
        self.assertEqual(selected.name, "fake-rust")

    def test_a_file_selects_a_server_by_extension(self):
        selected = server_for_path(Path("a.py"), self.servers)

        assert selected is not None
        self.assertEqual(selected.name, "fake-python")

    def test_an_unsupported_language_selects_nothing(self):
        self.assertIsNone(server_for_path(Path("a.zzz"), self.servers))

    def test_supported_languages_are_deduplicated(self):
        duplicated = (*self.servers, DiscoveredServer(definition=PYTHON, path="/bin/py2"))

        self.assertEqual(supported_languages(duplicated), ("python", "rust"))


class LspManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        (self.root / "a.py").write_bytes(b"def parse(raw):\n    return raw\n")
        self.addCleanup(self._directory.cleanup)

    def _manager(self, *, mode: str | None = None, executable=None) -> LspManager:
        env = os.environ.copy()
        if mode is not None:
            env["FAKE_LSP_MODE"] = mode

        def factory(server, root):
            del server
            return StdioTransport(
                executable or [sys.executable, str(SERVER)],
                cwd=root,
                env=env,
                request_timeout=5.0,
            )

        manager = LspManager(
            self.root,
            servers=(DiscoveredServer(definition=PYTHON, path="/bin/py"),),
            transport_factory=factory,
        )
        self.addAsyncCleanup(manager.stop_all)
        return manager

    async def test_a_client_is_started_for_a_supported_file(self):
        manager = self._manager()

        client = await manager.client_for(self.root / "a.py")

        self.assertTrue(client.initialized)
        self.assertEqual(manager.running, ("fake-python",))

    async def test_the_same_client_is_reused(self):
        manager = self._manager()

        first = await manager.client_for(self.root / "a.py")
        second = await manager.client_for(self.root / "a.py")

        self.assertIs(first, second)

    async def test_an_unsupported_file_reports_what_is_available(self):
        manager = self._manager()

        with self.assertRaises(LspUnavailableError) as caught:
            await manager.client_for(self.root / "a.zzz")

        self.assertEqual(caught.exception.code, "no_server")
        self.assertIn("python", caught.exception.message)

    async def test_a_server_that_cannot_start_is_reported(self):
        manager = self._manager(executable=["truecoder-no-such-server"])

        with self.assertRaises(LspUnavailableError) as caught:
            await manager.client_for(self.root / "a.py")

        self.assertEqual(caught.exception.code, "server_unavailable")

    async def test_a_failed_server_is_not_retried_on_every_call(self):
        manager = self._manager(executable=["truecoder-no-such-server"])

        with self.assertRaises(LspUnavailableError):
            await manager.client_for(self.root / "a.py")
        with self.assertRaises(LspUnavailableError) as caught:
            await manager.client_for(self.root / "a.py")

        self.assertEqual(caught.exception.code, "server_unavailable")

    async def test_stopping_clears_running_clients(self):
        manager = self._manager()
        await manager.client_for(self.root / "a.py")

        await manager.stop_all()

        self.assertEqual(manager.running, ())

    async def test_restarting_a_client_keeps_it_usable(self):
        manager = self._manager()
        await manager.client_for(self.root / "a.py")

        await manager.restart("fake-python")
        client = await manager.client_for(self.root / "a.py")

        self.assertTrue(client.initialized)

    async def test_concurrent_requests_start_one_server(self):
        import asyncio

        manager = self._manager()

        clients = await asyncio.gather(
            *(manager.client_for(self.root / "a.py") for _ in range(5))
        )

        self.assertEqual(len({id(client) for client in clients}), 1)


if __name__ == "__main__":
    unittest.main()
