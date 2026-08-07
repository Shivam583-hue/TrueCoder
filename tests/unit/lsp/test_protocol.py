from __future__ import annotations

import json
import unittest

from truecoder.lsp.protocol import (
    MAX_HEADER_BYTES,
    MessageBuffer,
    ProtocolError,
    encode_message,
    notification_message,
    request_message,
    response_error,
)


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class EncodeMessageTests(unittest.TestCase):
    def test_a_message_carries_its_byte_length(self):
        encoded = encode_message({"jsonrpc": "2.0", "method": "ping"})

        header, _, body = encoded.partition(b"\r\n\r\n")
        self.assertEqual(header, f"Content-Length: {len(body)}".encode())

    def test_the_length_counts_bytes_not_characters(self):
        encoded = encode_message({"text": "café"})

        header, _, body = encoded.partition(b"\r\n\r\n")
        self.assertIn(str(len(body)).encode(), header)
        self.assertEqual(json.loads(body)["text"], "café")

    def test_a_non_object_payload_is_refused(self):
        with self.assertRaises(ProtocolError):
            encode_message(["not", "an", "object"])  # type: ignore[arg-type]


class MessageShapeTests(unittest.TestCase):
    def test_a_request_carries_an_id_and_method(self):
        message = request_message(1, "initialize", {"rootUri": None})

        self.assertEqual(message["jsonrpc"], "2.0")
        self.assertEqual(message["id"], 1)
        self.assertEqual(message["method"], "initialize")
        self.assertEqual(message["params"], {"rootUri": None})

    def test_params_are_omitted_when_absent(self):
        self.assertNotIn("params", request_message(1, "shutdown"))

    def test_a_notification_has_no_id(self):
        message = notification_message("initialized", {})

        self.assertNotIn("id", message)
        self.assertEqual(message["method"], "initialized")

    def test_an_error_response_is_described(self):
        described = response_error(
            {"error": {"code": -32601, "message": "method not found"}}
        )

        self.assertIn("method not found", described)
        self.assertIn("-32601", described)

    def test_a_successful_response_has_no_error(self):
        self.assertIsNone(response_error({"result": {}}))


class MessageBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = MessageBuffer()

    def test_a_complete_message_is_returned(self):
        messages = self.buffer.feed(_frame({"id": 1, "result": "ok"}))

        self.assertEqual(messages, [{"id": 1, "result": "ok"}])

    def test_a_partial_header_yields_nothing(self):
        self.assertEqual(self.buffer.feed(b"Content-Len"), [])

    def test_a_partial_body_yields_nothing(self):
        frame = _frame({"id": 1, "result": "ok"})

        self.assertEqual(self.buffer.feed(frame[:-5]), [])

    def test_a_message_split_across_chunks_is_reassembled(self):
        frame = _frame({"id": 7, "result": "split"})

        self.assertEqual(self.buffer.feed(frame[:12]), [])
        self.assertEqual(self.buffer.feed(frame[12:20]), [])
        self.assertEqual(self.buffer.feed(frame[20:]), [{"id": 7, "result": "split"}])

    def test_several_messages_in_one_chunk_are_all_returned(self):
        chunk = _frame({"id": 1}) + _frame({"id": 2}) + _frame({"id": 3})

        messages = self.buffer.feed(chunk)

        self.assertEqual([m["id"] for m in messages], [1, 2, 3])

    def test_a_byte_at_a_time_still_parses(self):
        frame = _frame({"id": 9, "result": "slow"})
        received: list[dict] = []

        for index in range(len(frame)):
            received.extend(self.buffer.feed(frame[index : index + 1]))

        self.assertEqual(received, [{"id": 9, "result": "slow"}])

    def test_extra_headers_are_tolerated(self):
        body = json.dumps({"id": 4}).encode()
        frame = (
            b"Content-Length: "
            + str(len(body)).encode()
            + b"\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
            + body
        )

        self.assertEqual(self.buffer.feed(frame), [{"id": 4}])

    def test_header_names_are_case_insensitive(self):
        body = json.dumps({"id": 5}).encode()
        frame = b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body

        self.assertEqual(self.buffer.feed(frame), [{"id": 5}])

    def test_a_missing_content_length_is_refused(self):
        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(b"Content-Type: text/plain\r\n\r\n{}")

        self.assertEqual(caught.exception.code, "invalid_header")

    def test_a_non_numeric_content_length_is_refused(self):
        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(b"Content-Length: many\r\n\r\n{}")

        self.assertEqual(caught.exception.code, "invalid_header")

    def test_a_negative_content_length_is_refused(self):
        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(b"Content-Length: -5\r\n\r\n{}")

        self.assertEqual(caught.exception.code, "invalid_header")

    def test_an_oversized_message_is_refused(self):
        buffer = MessageBuffer(max_message_bytes=16)

        with self.assertRaises(ProtocolError) as caught:
            buffer.feed(b"Content-Length: 1024\r\n\r\n")

        self.assertEqual(caught.exception.code, "message_too_large")

    def test_an_endless_header_is_refused(self):
        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(b"x" * (MAX_HEADER_BYTES + 1))

        self.assertEqual(caught.exception.code, "header_too_large")

    def test_an_invalid_body_is_refused(self):
        frame = b"Content-Length: 3\r\n\r\nnot"

        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(frame)

        self.assertEqual(caught.exception.code, "invalid_body")

    def test_a_non_object_body_is_refused(self):
        body = b"[1,2]"
        frame = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body

        with self.assertRaises(ProtocolError) as caught:
            self.buffer.feed(frame)

        self.assertEqual(caught.exception.code, "invalid_body")

    def test_a_round_trip_through_encode_parses_back(self):
        payload = request_message(11, "textDocument/definition", {"x": 1})

        self.assertEqual(self.buffer.feed(encode_message(payload)), [payload])


if __name__ == "__main__":
    unittest.main()
