from __future__ import annotations

from tests.helpers.platforms import skip_module_on_windows

skip_module_on_windows('the POSIX supervisor protocol')

import asyncio
import os
import struct
import unittest

from truecoder.execution.backends.posix_protocol import (
    MAX_POSIX_FRAME_BYTES,
    decode_frame,
    encode_frame,
    read_frame_fd,
    read_frame_stream,
    write_frame_async,
    write_frame_fd,
)


class PosixProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_pipe_round_trip(self):
        read_fd, write_fd = os.pipe()
        try:
            write_frame_fd(
                write_fd,
                "READY",
                {"supervisor_pid": 10, "project_pgid": 11},
            )
            frame = read_frame_fd(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        self.assertEqual(frame.type, "READY")
        self.assertEqual(frame.payload["project_pgid"], 11)

    async def test_async_writer_and_fragmented_stream_reader(self):
        read_fd, write_fd = os.pipe()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol,
            os.fdopen(read_fd, "rb", buffering=0),
        )
        try:
            await write_frame_async(write_fd, "START", {})
            frame = await read_frame_stream(reader)
        finally:
            os.close(write_fd)
            transport.close()

        self.assertEqual(frame.type, "START")

    def test_duplicate_unknown_and_wrong_version_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            decode_frame(
                b'{"version":1,"version":1,"type":"START","payload":{}}'
            )
        with self.assertRaisesRegex(ValueError, "fields"):
            decode_frame(
                b'{"version":1,"type":"START","payload":{},"extra":true}'
            )
        with self.assertRaisesRegex(ValueError, "version"):
            decode_frame(b'{"version":2,"type":"START","payload":{}}')

    def test_payload_schema_is_strict(self):
        with self.assertRaisesRegex(ValueError, "fields"):
            encode_frame("START", {"unexpected": True})
        with self.assertRaisesRegex(ValueError, "positive"):
            encode_frame(
                "READY",
                {"supervisor_pid": 0, "project_pgid": 1},
            )
        with self.assertRaisesRegex(ValueError, "grace"):
            encode_frame(
                "TERMINATE",
                {"reason": "timeout", "grace_seconds": -1},
            )

    def test_oversized_length_is_rejected_before_body_read(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, struct.pack("!I", MAX_POSIX_FRAME_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size"):
                read_frame_fd(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_environment_values_do_not_appear_in_decode_errors(self):
        secret = "never-print-this-secret"
        malformed = (
            b'{"version":1,"type":"CONFIG","payload":{"environment":[["X","'
            + secret.encode()
            + b'"]]},"extra":true}'
        )

        with self.assertRaises(ValueError) as raised:
            decode_frame(malformed)

        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
