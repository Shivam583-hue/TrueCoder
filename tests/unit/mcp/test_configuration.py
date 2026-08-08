"""Server configuration is strict and fails closed rather than guessing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from truecoder.mcp.configuration import (
    DEFAULT_STARTUP_TIMEOUT,
    MAX_SERVERS,
    McpConfigError,
    McpServer,
    load_mcp_servers,
    parse_mcp_servers,
    resolve_working_directory,
)


def _config(**overrides) -> str:
    server = {"name": "files", "command": ["mcp-files"]}
    server.update(overrides)
    return json.dumps({"version": 1, "servers": [server]})


class ParseTests(unittest.TestCase):
    def test_a_minimal_server_is_accepted(self):
        suite = parse_mcp_servers(_config())

        self.assertTrue(suite.available)
        self.assertEqual(suite.servers[0].name, "files")
        self.assertEqual(suite.servers[0].command, ("mcp-files",))
        self.assertEqual(
            suite.servers[0].startup_timeout_seconds,
            DEFAULT_STARTUP_TIMEOUT,
        )

    def test_every_field_is_carried(self):
        suite = parse_mcp_servers(
            _config(
                environment={"TOKEN": "abc"},
                working_directory="servers",
                startup_timeout_seconds=5,
            )
        )

        server = suite.servers[0]
        self.assertEqual(server.environment, (("TOKEN", "abc"),))
        self.assertEqual(server.working_directory, "servers")
        self.assertEqual(server.startup_timeout_seconds, 5.0)

    def test_no_servers_is_valid_and_unavailable(self):
        suite = parse_mcp_servers(json.dumps({"version": 1, "servers": []}))

        self.assertIsNone(suite.unavailable_reason)
        self.assertFalse(suite.available)

    def test_an_unknown_root_field_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(json.dumps({"version": 1, "servers": [], "extra": 1}))

    def test_an_unknown_server_field_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(_config(surprise=True))

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(json.dumps({"version": 2, "servers": []}))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers("{not json")

    def test_a_non_object_configuration_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(json.dumps([1, 2]))

    def test_a_missing_command_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(json.dumps({"version": 1, "servers": [{"name": "a"}]}))

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(_config(command=[]))

    def test_an_unusable_server_name_is_refused(self):
        for name in ("has space", "", "dots.here"):
            with self.subTest(name=name), self.assertRaises(McpConfigError):
                parse_mcp_servers(_config(name=name))

    def test_duplicate_server_names_are_refused(self):
        payload = json.dumps(
            {
                "version": 1,
                "servers": [
                    {"name": "files", "command": ["a"]},
                    {"name": "files", "command": ["b"]},
                ],
            }
        )

        with self.assertRaises(McpConfigError):
            parse_mcp_servers(payload)

    def test_too_many_servers_are_refused(self):
        payload = json.dumps(
            {
                "version": 1,
                "servers": [
                    {"name": f"s{index}", "command": ["a"]}
                    for index in range(MAX_SERVERS + 1)
                ],
            }
        )

        with self.assertRaises(McpConfigError):
            parse_mcp_servers(payload)

    def test_an_absolute_working_directory_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(_config(working_directory="/etc"))

    def test_a_non_text_environment_value_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(_config(environment={"TOKEN": 5}))

    def test_a_non_positive_timeout_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(_config(startup_timeout_seconds=0))

    def test_an_oversized_configuration_is_refused(self):
        with self.assertRaises(McpConfigError):
            parse_mcp_servers(" " * (64 * 1024 + 1))


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_a_missing_file_means_no_servers(self):
        suite = load_mcp_servers(self.root / "absent.json")

        self.assertEqual(suite.servers, ())
        self.assertIsNone(suite.unavailable_reason)

    def test_a_broken_file_is_reported_and_never_raised(self):
        path = self.root / "mcp.json"
        path.write_text("{not json", encoding="utf-8")

        suite = load_mcp_servers(path)

        self.assertEqual(suite.servers, ())
        self.assertIsNotNone(suite.unavailable_reason)
        self.assertFalse(suite.available)

    def test_a_good_file_is_loaded(self):
        path = self.root / "mcp.json"
        path.write_text(_config(), encoding="utf-8")

        suite = load_mcp_servers(path)

        self.assertTrue(suite.available)

    def test_a_non_path_is_rejected(self):
        with self.assertRaises(McpConfigError):
            load_mcp_servers("mcp.json")  # type: ignore[arg-type]


class WorkingDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_the_workspace_root_resolves(self):
        self.assertEqual(resolve_working_directory(self.root, "."), self.root)

    def test_a_nested_directory_resolves(self):
        (self.root / "servers").mkdir()

        resolved = resolve_working_directory(self.root, "servers")

        self.assertEqual(resolved, self.root / "servers")

    def test_an_escape_is_refused(self):
        for requested in ("..", "../elsewhere", "servers/../.."):
            with self.subTest(requested=requested), self.assertRaises(McpConfigError):
                resolve_working_directory(self.root, requested)

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(McpConfigError):
            resolve_working_directory(self.root, "/etc")


class ServerModelTests(unittest.TestCase):
    def test_a_command_part_that_is_blank_is_refused(self):
        with self.assertRaises(McpConfigError):
            McpServer(name="files", command=("mcp", "  "))

    def test_too_many_command_parts_are_refused(self):
        with self.assertRaises(McpConfigError):
            McpServer(name="files", command=tuple(str(n) for n in range(64)))


if __name__ == "__main__":
    unittest.main()
