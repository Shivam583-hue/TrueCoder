from __future__ import annotations

import hashlib
import random
import unittest
from itertools import pairwise

from truecoder.execution.models import ExecutionLimits
from truecoder.execution.output import (
    REDACTION_MARKER,
    BoundedByteStream,
    OutputCollector,
    StreamingValueRedactor,
    TerminalSanitizer,
)


def limits(**overrides: object) -> ExecutionLimits:
    values: dict[str, object] = {
        "timeout_seconds": 30.0,
        "max_output_bytes": 1024,
        "max_return_bytes": 128,
        "memory_bytes": None,
        "cpu_seconds": None,
        "max_processes": None,
        "termination_grace_seconds": 1.0,
    }
    values.update(overrides)
    return ExecutionLimits(**values)  # type: ignore[arg-type]


def partitions(payload: bytes, cuts: tuple[int, ...]) -> tuple[bytes, ...]:
    boundaries = (0, *cuts, len(payload))
    return tuple(payload[start:end] for start, end in pairwise(boundaries))


def collect_live_text(
    payload: bytes,
    chunks: tuple[bytes, ...],
    *,
    redaction_values: tuple[str, ...] = (),
) -> tuple[str, OutputCollector]:
    output_limit = max(1, len(payload) * 2)
    collector = OutputCollector(
        limits(
            max_output_bytes=output_limit,
            max_return_bytes=min(128, output_limit),
        ),
        redaction_values=redaction_values,
    )
    text = "".join(collector.feed_stdout(chunk).text for chunk in chunks)
    text += collector.close_stdout().text
    collector.close_stderr()
    return text, collector


class BoundedByteStreamTests(unittest.TestCase):
    def test_hashes_every_byte_and_retains_only_fixed_head_and_tail(self):
        stream = BoundedByteStream(8)
        stream.feed(b"0123")
        stream.feed(b"456789abcdef")
        stream.finish()
        snapshot = stream.snapshot()

        self.assertEqual(snapshot.total_bytes, 16)
        self.assertEqual(
            snapshot.sha256,
            hashlib.sha256(b"0123456789abcdef").hexdigest(),
        )
        self.assertEqual(snapshot.head, b"0123")
        self.assertEqual(snapshot.tail, b"cdef")
        self.assertLessEqual(snapshot.retained_bytes, 8)
        self.assertTrue(snapshot.finished)

    def test_retained_result_is_independent_of_chunk_boundaries(self):
        payload = bytes(range(256)) * 8
        expected = BoundedByteStream(79)
        expected.feed(payload)

        generator = random.Random(1)
        for _ in range(100):
            actual = BoundedByteStream(79)
            cursor = 0
            while cursor < len(payload):
                size = generator.randint(1, 91)
                actual.feed(payload[cursor : cursor + size])
                cursor += size
            self.assertEqual(actual.snapshot(), expected.snapshot())

    def test_large_total_output_does_not_increase_retained_memory(self):
        stream = BoundedByteStream(128)
        for _ in range(20_000):
            stream.feed(b"x" * 100)

        self.assertEqual(stream.total_bytes, 2_000_000)
        self.assertLessEqual(stream.retained_bytes, 128)


class StreamingTextTests(unittest.TestCase):
    def test_incremental_unicode_is_identical_at_every_byte_boundary(self):
        payload = "start → café 🤖 समाप्त\n".encode()
        expected, _collector = collect_live_text(payload, (payload,))

        for cut in range(len(payload) + 1):
            with self.subTest(cut=cut):
                actual, _collector = collect_live_text(
                    payload,
                    partitions(payload, (cut,)),
                )
                self.assertEqual(actual, expected)

    def test_terminal_sequences_are_removed_across_chunk_boundaries(self):
        text = "before\x1b[31mred\x1b[0m\x1b]0;secret title\x07after\r\nnext\x00line"
        payload = text.encode()
        expected = "beforeredafter\nnextline"

        for cut in range(len(payload) + 1):
            sanitizer = TerminalSanitizer()
            chunks = partitions(payload, (cut,))
            actual = "".join(sanitizer.feed(chunk.decode("latin1")) for chunk in chunks)
            actual += sanitizer.feed("", final=True)
            with self.subTest(cut=cut):
                self.assertEqual(actual, expected)

    def test_redaction_never_leaks_a_value_split_between_chunks(self):
        secret = "top-secret-value"
        text = f"before {secret} after {secret}"
        expected = f"before {REDACTION_MARKER} after {REDACTION_MARKER}"

        for cut in range(len(text) + 1):
            redactor = StreamingValueRedactor((secret,))
            actual = redactor.feed(text[:cut])
            actual += redactor.feed(text[cut:], final=True)
            with self.subTest(cut=cut):
                self.assertEqual(actual, expected)
                self.assertNotIn(secret, actual)

    def test_random_chunking_preserves_sanitized_redacted_live_output(self):
        secret = "never-show-this"
        payload = (f"α\x1b[33mwarning\x1b[0m:{secret}\r\nfinal 🤖").encode()
        expected, _collector = collect_live_text(
            payload,
            (payload,),
            redaction_values=(secret,),
        )
        generator = random.Random(91)

        for _ in range(200):
            chunks: list[bytes] = []
            cursor = 0
            while cursor < len(payload):
                size = generator.randint(1, 8)
                chunks.append(payload[cursor : cursor + size])
                cursor += size
            actual, _collector = collect_live_text(
                payload,
                tuple(chunks),
                redaction_values=(secret,),
            )
            self.assertEqual(actual, expected)
            self.assertNotIn(secret, actual)


class OutputCollectorTests(unittest.TestCase):
    def test_final_bounded_result_is_independent_of_chunk_boundaries(self):
        secret = "not-a-real-secret"
        payload = (
            f"head α \x1b[31mred\x1b[0m {secret} " + "middle-" * 100 + "tail 🤖"
        ).encode()

        def collect(chunks: tuple[bytes, ...]):
            collector = OutputCollector(
                limits(
                    max_output_bytes=len(payload) * 2,
                    max_return_bytes=96,
                ),
                redaction_values=(secret,),
            )
            for chunk in chunks:
                collector.feed_stdout(chunk)
            collector.close_stdout()
            collector.close_stderr()
            return collector.snapshot()

        expected = collect((payload,))
        generator = random.Random(123)
        for _ in range(100):
            chunks: list[bytes] = []
            cursor = 0
            while cursor < len(payload):
                size = generator.randint(1, 31)
                chunks.append(payload[cursor : cursor + size])
                cursor += size
            self.assertEqual(collect(tuple(chunks)), expected)
            self.assertNotIn(secret, expected.stdout.text)

    def test_counts_streams_digests_and_allocates_one_return_budget(self):
        collector = OutputCollector(
            limits(max_output_bytes=10_000, max_return_bytes=80)
        )
        stdout = b"a" * 200
        stderr = b"b" * 100
        collector.feed_stdout(stdout)
        collector.feed_stderr(stderr)
        collector.close_stdout()
        collector.close_stderr()

        snapshot = collector.snapshot()

        self.assertEqual(snapshot.stdout.byte_count, len(stdout))
        self.assertEqual(snapshot.stderr.byte_count, len(stderr))
        self.assertEqual(
            snapshot.stdout.sha256,
            hashlib.sha256(stdout).hexdigest(),
        )
        self.assertEqual(
            snapshot.stderr.sha256,
            hashlib.sha256(stderr).hexdigest(),
        )
        self.assertLessEqual(
            len(snapshot.stdout.text.encode()) + len(snapshot.stderr.text.encode()),
            80,
        )
        self.assertTrue(snapshot.stdout.truncated)
        self.assertTrue(snapshot.stderr.truncated)
        self.assertTrue(snapshot.complete)

    def test_output_limit_crossing_is_reported_exactly_once(self):
        collector = OutputCollector(limits(max_output_bytes=5, max_return_bytes=5))

        first = collector.feed_stdout(b"12345")
        second = collector.feed_stderr(b"6")
        third = collector.feed_stdout(b"7")

        self.assertFalse(first.limit_exceeded)
        self.assertTrue(second.limit_exceeded)
        self.assertTrue(second.newly_exceeded)
        self.assertTrue(third.limit_exceeded)
        self.assertFalse(third.newly_exceeded)

    def test_invalid_utf8_is_replaced_and_bounded(self):
        collector = OutputCollector(limits(max_output_bytes=100, max_return_bytes=16))
        update = collector.feed_stdout(b"\xffhello")
        update_text = update.text + collector.close_stdout().text
        collector.close_stderr()
        snapshot = collector.snapshot()

        self.assertIn("\ufffd", update_text)
        self.assertLessEqual(len(snapshot.stdout.text.encode()), 16)

    def test_collector_memory_stays_bounded_when_output_is_unbounded(self):
        collector = OutputCollector(
            limits(max_output_bytes=256, max_return_bytes=128),
            redaction_values=("not-a-real-secret",),
        )
        for _ in range(10_000):
            collector.feed_stdout(b"x" * 1024)

        self.assertEqual(collector.combined_bytes, 10_240_000)
        self.assertLessEqual(collector.retained_bytes, 128)
        self.assertLess(
            collector.pending_text_characters,
            len("not-a-real-secret") + 4,
        )

    def test_streams_have_explicit_close_ownership(self):
        collector = OutputCollector(limits())
        collector.close_stdout()

        with self.assertRaises(RuntimeError):
            collector.close_stdout()
        with self.assertRaises(RuntimeError):
            collector.feed_stdout(b"late")
        self.assertFalse(collector.snapshot().complete)


if __name__ == "__main__":
    unittest.main()
