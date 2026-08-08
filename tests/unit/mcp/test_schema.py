"""A server's schema is untrusted input and must be bounded before it is used."""

from __future__ import annotations

import unittest

from truecoder.mcp.schema import (
    MAX_DESCRIPTION_LENGTH,
    MAX_ENUM_VALUES,
    MAX_PROPERTIES,
    MAX_SCHEMA_DEPTH,
    SchemaRejected,
    bound_text,
    bound_tool_schema,
)


def _nested(depth: int) -> dict:
    schema: dict = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"child": schema}}
    return schema


class BoundSchemaTests(unittest.TestCase):
    def test_an_ordinary_schema_survives(self):
        bounded = bound_tool_schema(
            {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 20}},
                "required": ["text"],
            }
        )

        self.assertEqual(bounded["type"], "object")
        self.assertEqual(bounded["properties"]["text"]["type"], "string")
        self.assertEqual(bounded["properties"]["text"]["maxLength"], 20)
        self.assertEqual(bounded["required"], ["text"])

    def test_additional_properties_is_always_closed(self):
        bounded = bound_tool_schema(
            {"type": "object", "properties": {}, "additionalProperties": True}
        )

        self.assertFalse(bounded["additionalProperties"])

    def test_an_unsupported_type_is_refused(self):
        with self.assertRaises(SchemaRejected):
            bound_tool_schema({"type": "function"})

    def test_a_non_object_tool_schema_is_refused(self):
        with self.assertRaises(SchemaRejected):
            bound_tool_schema({"type": "string"})

    def test_a_schema_that_is_not_an_object_is_refused(self):
        for value in ("text", 3, None, []):
            with self.subTest(value=value), self.assertRaises(SchemaRejected):
                bound_tool_schema(value)

    def test_deep_nesting_is_refused(self):
        with self.assertRaises(SchemaRejected):
            bound_tool_schema(_nested(MAX_SCHEMA_DEPTH + 2))

    def test_nesting_within_the_limit_is_kept(self):
        bounded = bound_tool_schema(_nested(3))

        self.assertEqual(bounded["type"], "object")

    def test_too_many_properties_is_refused(self):
        properties = {
            f"p{index}": {"type": "string"} for index in range(MAX_PROPERTIES + 1)
        }

        with self.assertRaises(SchemaRejected):
            bound_tool_schema({"type": "object", "properties": properties})

    def test_too_many_enum_values_is_refused(self):
        with self.assertRaises(SchemaRejected):
            bound_tool_schema(
                {
                    "type": "object",
                    "properties": {
                        "choice": {
                            "type": "string",
                            "enum": [str(n) for n in range(MAX_ENUM_VALUES + 1)],
                        }
                    },
                }
            )

    def test_unknown_keywords_are_dropped(self):
        bounded = bound_tool_schema(
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "$ref": "http://example.invalid/schema",
                "allOf": [{"type": "object"}],
                "x-vendor": {"anything": True},
            }
        )

        self.assertNotIn("$ref", bounded)
        self.assertNotIn("allOf", bounded)
        self.assertNotIn("x-vendor", bounded)

    def test_a_required_name_without_a_property_is_dropped(self):
        bounded = bound_tool_schema(
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text", "invented"],
            }
        )

        self.assertEqual(bounded["required"], ["text"])

    def test_a_long_description_is_shortened(self):
        bounded = bound_tool_schema(
            {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "d" * 9000}},
            }
        )

        self.assertLessEqual(
            len(bounded["properties"]["text"]["description"]),
            MAX_DESCRIPTION_LENGTH,
        )

    def test_an_object_without_properties_still_declares_them(self):
        bounded = bound_tool_schema({"type": "object"})

        self.assertEqual(bounded["properties"], {})


class BoundTextTests(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(bound_text("a\n\n  b\tc", 100), "a b c")

    def test_a_non_string_becomes_empty(self):
        for value in (None, 3, [], {}):
            with self.subTest(value=value):
                self.assertEqual(bound_text(value, 100), "")

    def test_the_limit_is_respected(self):
        self.assertEqual(len(bound_text("x" * 500, 10)), 10)


if __name__ == "__main__":
    unittest.main()
