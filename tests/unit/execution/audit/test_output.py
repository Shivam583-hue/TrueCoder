from __future__ import annotations

import hashlib
import unittest

from truecoder.execution.audit.output import BoundedOutputEvidence


class BoundedOutputEvidenceTests(unittest.TestCase):
    def test_hashes_all_bytes_while_retaining_first_and_last_preview(self):
        collector = BoundedOutputEvidence(max_preview_bytes=8)
        collector.add_stdout(b"0123")
        collector.add_stdout(b"456789abcdef")
        evidence = collector.snapshot()

        self.assertEqual(evidence.stdout_bytes, 16)
        self.assertEqual(
            evidence.stdout_sha256,
            hashlib.sha256(b"0123456789abcdef").hexdigest(),
        )
        self.assertTrue(evidence.stdout_truncated)
        self.assertTrue(evidence.stdout_preview.startswith("01"))
        self.assertTrue(evidence.stdout_preview.endswith("def"))
        self.assertNotIn("456789ab", evidence.stdout_preview)
        self.assertLessEqual(len(evidence.stdout_preview.encode()), 8)

    def test_tracks_streams_independently_and_marks_incomplete_collection(self):
        collector = BoundedOutputEvidence(max_preview_bytes=32)
        collector.add_stdout("héllo".encode())
        collector.add_stderr(b"warning")
        collector.mark_incomplete()
        evidence = collector.snapshot()

        self.assertEqual(evidence.stdout_preview, "héllo")
        self.assertEqual(evidence.stderr_preview, "warning")
        self.assertFalse(evidence.complete)
        self.assertFalse(evidence.stdout_truncated)

    def test_rejects_non_byte_chunks(self):
        collector = BoundedOutputEvidence()
        with self.assertRaises(TypeError):
            collector.add_stdout("not bytes")  # type: ignore[arg-type]

    def test_preview_limit_cannot_exceed_durable_model_bound(self):
        with self.assertRaises(ValueError):
            BoundedOutputEvidence(max_preview_bytes=128 * 1024 + 1)

    def test_invalid_utf8_and_truncation_marker_remain_inside_byte_budget(self):
        collector = BoundedOutputEvidence(max_preview_bytes=128 * 1024)
        collector.add_stdout(b"\xff" * (256 * 1024))

        evidence = collector.snapshot()

        self.assertTrue(evidence.stdout_truncated)
        self.assertLessEqual(
            len(evidence.stdout_preview.encode("utf-8")),
            128 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
