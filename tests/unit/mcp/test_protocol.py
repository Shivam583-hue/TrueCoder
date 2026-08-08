"""Newline framing must round trip and refuse anything oversized or malformed."""

from __future__ import annotations

import json
import unittest

from truecoder.jsonrpc.framing import ProtocolError
from truecoder.mcp.protocol import (
    PROTOCOL_VERSION,
    LineBuffer,
    LineFraming,
    call_tool_params,
    initialize_params,
    server_protocol_version,
)


class LineFramingTests(unittest.TestCase):
    def test_a_message_round_trips(self):
        framing = LineFraming()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

        encoded = framing.encode(payload)

        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(framing.reader().feed(encoded), [payload])

    def test_several_messages_in_one_chunk_are_all_read(self):
        framing = LineFraming()
        first = {"jsonrpc": "2.0", "id": 1, "method": "a"}
        second = {"jsonrpc": "2.0", "id": 2, "method": "b"}

        messages = framing.reader().feed(framing.encode(first) + framing.encode(second))

        self.assertEqual(messages, [first, second])

    def test_a_message_split_across_chunks_is_reassembled(self):
        framing = LineFraming()
        encoded = framing.encode({"jsonrpc": "2.0", "id": 1, "method": "a"})
        reader = framing.reader()

        self.assertEqual(reader.feed(encoded[:5]), [])
        self.assertEqual(len(reader.feed(encoded[5:])), 1)

    def test_blank_lines_are_ignored(self):
        reader = LineBuffer()

        messages = reader.feed(b'\n\n{"jsonrpc": "2.0", "id": 1}\n\n')

        self.assertEqual(messages, [{"jsonrpc": "2.0", "id": 1}])

    def test_a_body_that_is_not_json_is_refused(self):
        with self.assertRaises(ProtocolError):
            LineBuffer().feed(b"not json at all\n")

    def test_a_body_that_is_not_an_object_is_refused(self):
        with self.assertRaises(ProtocolError):
            LineBuffer().feed(b"[1, 2, 3]\n")

    def test_an_oversized_message_is_refused_before_it_completes(self):
        reader = LineBuffer(max_message_bytes=64)

        with self.assertRaises(ProtocolError):
            reader.feed(b"x" * 200)

    def test_an_oversized_message_cannot_be_sent(self):
        framing = LineFraming(max_message_bytes=64)

        with self.assertRaises(ProtocolError):
            framing.encode({"jsonrpc": "2.0", "text": "x" * 500})

    def test_a_zero_limit_is_rejected(self):
        for factory in (LineFraming, LineBuffer):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory(max_message_bytes=0)

    def test_unicode_survives_the_round_trip(self):
        framing = LineFraming()
        payload = {"jsonrpc": "2.0", "text": "héllo → wörld"}

        self.assertEqual(framing.reader().feed(framing.encode(payload)), [payload])

    def test_a_newline_inside_a_string_never_splits_a_message(self):
        framing = LineFraming()
        payload = {"jsonrpc": "2.0", "text": "first\nsecond"}

        encoded = framing.encode(payload)

        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(framing.reader().feed(encoded), [payload])


class MessageBuilderTests(unittest.TestCase):
    def test_initialize_states_the_protocol_version(self):
        params = initialize_params()

        self.assertEqual(params["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("clientInfo", params)

    def test_a_tool_call_carries_its_name_and_arguments(self):
        params = call_tool_params("echo", {"text": "hi"})

        self.assertEqual(params, {"name": "echo", "arguments": {"text": "hi"}})

    def test_an_empty_tool_name_is_rejected(self):
        with self.assertRaises(ValueError):
            call_tool_params("  ", {})

    def test_non_object_arguments_are_rejected(self):
        with self.assertRaises(TypeError):
            call_tool_params("echo", ["text"])  # type: ignore[arg-type]

    def test_a_server_without_a_protocol_version_is_refused(self):
        with self.assertRaises(ProtocolError):
            server_protocol_version({"capabilities": {}})

    def test_a_server_protocol_version_is_returned(self):
        self.assertEqual(
            server_protocol_version(json.loads('{"protocolVersion": "2025-06-18"}')),
            "2025-06-18",
        )


if __name__ == "__main__":
    unittest.main()
