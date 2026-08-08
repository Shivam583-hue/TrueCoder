"""A server's tool listing and results are untrusted and must be parsed defensively."""

from __future__ import annotations

import unittest

from truecoder.mcp.models import (
    MAX_RESULT_CHARACTERS,
    MAX_TOOLS_PER_SERVER,
    parse_tool_descriptors,
    parse_tool_result,
    usable_tool_name,
)

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


def _listing(*tools: dict) -> dict:
    return {"tools": list(tools)}


def _tool(name: str, **overrides) -> dict:
    entry = {"name": name, "description": "does a thing", "inputSchema": SCHEMA}
    entry.update(overrides)
    return entry


class ToolNameTests(unittest.TestCase):
    def test_ordinary_names_are_usable(self):
        for name in ("echo", "read_file", "get-weather", "tool2"):
            with self.subTest(name=name):
                self.assertTrue(usable_tool_name(name))

    def test_unusable_names_are_refused(self):
        for name in ("", "   ", "has space", "dots.here", "sla/sh", None, 3):
            with self.subTest(name=name):
                self.assertFalse(usable_tool_name(name))


class ParseDescriptorsTests(unittest.TestCase):
    def test_a_well_formed_listing_is_kept(self):
        tools = parse_tool_descriptors(_listing(_tool("echo")))

        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "echo")
        self.assertFalse(tools[0].schema["additionalProperties"])

    def test_a_tool_with_an_unusable_name_is_skipped(self):
        tools = parse_tool_descriptors(_listing(_tool("bad name"), _tool("echo")))

        self.assertEqual([tool.name for tool in tools], ["echo"])

    def test_a_tool_with_a_rejected_schema_is_skipped(self):
        tools = parse_tool_descriptors(
            _listing(_tool("weird", inputSchema={"type": "function"}), _tool("echo"))
        )

        self.assertEqual([tool.name for tool in tools], ["echo"])

    def test_a_snake_case_schema_key_is_accepted(self):
        tools = parse_tool_descriptors(
            _listing({"name": "echo", "input_schema": SCHEMA})
        )

        self.assertEqual(len(tools), 1)

    def test_the_number_of_tools_is_bounded(self):
        listing = _listing(
            *(_tool(f"tool{index}") for index in range(MAX_TOOLS_PER_SERVER + 10))
        )

        self.assertEqual(len(parse_tool_descriptors(listing)), MAX_TOOLS_PER_SERVER)

    def test_a_malformed_listing_yields_nothing(self):
        for payload in (None, [], {"tools": "many"}, {}, 7):
            with self.subTest(payload=payload):
                self.assertEqual(parse_tool_descriptors(payload), ())

    def test_a_non_object_entry_is_skipped(self):
        tools = parse_tool_descriptors(_listing("echo", _tool("real")))

        self.assertEqual([tool.name for tool in tools], ["real"])


class ParseResultTests(unittest.TestCase):
    def test_text_blocks_are_joined(self):
        result = parse_tool_result(
            {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ]
            }
        )

        self.assertEqual(result.text, "first\nsecond")
        self.assertFalse(result.is_error)

    def test_a_non_text_block_is_named_but_not_included(self):
        result = parse_tool_result(
            {"content": [{"type": "image", "data": "iVBORw0KGgo="}]}
        )

        self.assertEqual(result.text, "[image content omitted]")

    def test_an_error_flag_is_carried(self):
        result = parse_tool_result(
            {"content": [{"type": "text", "text": "no"}], "isError": True}
        )

        self.assertTrue(result.is_error)

    def test_a_snake_case_error_flag_is_carried(self):
        result = parse_tool_result({"content": [], "is_error": True})

        self.assertTrue(result.is_error)

    def test_an_enormous_result_is_bounded_and_says_so(self):
        result = parse_tool_result(
            {"content": [{"type": "text", "text": "y" * (MAX_RESULT_CHARACTERS * 2)}]}
        )

        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), MAX_RESULT_CHARACTERS)

    def test_a_malformed_result_is_an_error_rather_than_a_crash(self):
        for payload in (None, [], "text", 9):
            with self.subTest(payload=payload):
                result = parse_tool_result(payload)
                self.assertTrue(result.is_error)
                self.assertEqual(result.text, "")


if __name__ == "__main__":
    unittest.main()
