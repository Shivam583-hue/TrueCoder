from __future__ import annotations

import json
import random
import unittest

from truecoder.execution.backends.windows_plan import (
    build_command_line,
    normalize_exit_code,
    normalize_start_error,
    quote_argument,
)
from truecoder.execution.output import BoundedByteStream, TerminalSanitizer
from truecoder.execution.trusted_rules import TrustedRulesError, parse_trusted_rules
from truecoder.tui.execution_view import BoundedPreview

SEED = 20260804
ROUNDS = 400

_ALPHABET = (
    "abzAZ09 \t\n\r\\\"'`$&|;<>*?()[]{}#!%^~+=,.:/\x00\x1b[0m"
    "é中\U0001f600"
)


def noisy_text(rng: random.Random, maximum: int = 64) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(maximum)))


class SanitizerFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(SEED)

    def test_sanitizer_never_emits_control_sequences(self):
        sanitizer = TerminalSanitizer()
        for _ in range(ROUNDS):
            cleaned = sanitizer.feed(noisy_text(self.rng), final=False)
            self.assertNotIn("\x1b", cleaned)
            self.assertNotIn("\x00", cleaned)

    def test_sanitizer_is_stable_across_chunk_boundaries(self):
        for _ in range(ROUNDS):
            text = noisy_text(self.rng, maximum=96)
            whole = TerminalSanitizer().feed(text, final=True)

            chunked = TerminalSanitizer()
            split = self.rng.randrange(len(text) + 1) if text else 0
            pieces = chunked.feed(text[:split]) + chunked.feed(
                text[split:],
                final=True,
            )

            self.assertEqual(pieces, whole)


class BoundedStreamFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(SEED + 1)

    def test_byte_counts_and_digests_ignore_chunking(self):
        for _ in range(200):
            payload = bytes(
                self.rng.randrange(256) for _ in range(self.rng.randrange(300))
            )
            whole = BoundedByteStream(32)
            whole.feed(payload)

            chunked = BoundedByteStream(32)
            index = 0
            while index < len(payload):
                step = self.rng.randrange(1, 17)
                chunked.feed(payload[index : index + step])
                index += step

            self.assertEqual(chunked.total_bytes, whole.total_bytes)
            self.assertEqual(chunked.snapshot(), whole.snapshot())


class PreviewFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(SEED + 2)

    def test_preview_stays_bounded_for_any_input(self):
        for _ in range(ROUNDS):
            preview = BoundedPreview(max_lines=8, max_line_chars=32)
            for _ in range(self.rng.randrange(30)):
                preview.append(noisy_text(self.rng))

            lines = preview.text().splitlines()
            self.assertLessEqual(len(lines), 9)
            for line in lines:
                self.assertLessEqual(len(line), 64)

    def test_preview_never_raises_on_arbitrary_text(self):
        preview = BoundedPreview()
        for _ in range(ROUNDS):
            preview.append(noisy_text(self.rng))


class TrustedRulesFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(SEED + 3)

    def test_arbitrary_documents_never_crash_the_parser(self):
        for _ in range(ROUNDS):
            candidate = self.rng.choice(
                (
                    noisy_text(self.rng),
                    json.dumps(self.rng.choice(([], {}, 3, "text", None))),
                    json.dumps(
                        {
                            "version": self.rng.randrange(-2, 4),
                            "rules": [
                                {
                                    "rule_id": noisy_text(self.rng, 12),
                                    "executable": noisy_text(self.rng, 12),
                                    "max_risk": self.rng.choice(
                                        ("low", "medium", "high", "bogus")
                                    ),
                                }
                            ],
                        }
                    ),
                )
            )
            try:
                parse_trusted_rules(candidate)
            except TrustedRulesError:
                continue

    def test_a_parsed_document_always_satisfies_its_invariants(self):
        for _ in range(ROUNDS):
            document = json.dumps(
                {
                    "version": 1,
                    "rules": [
                        {
                            "rule_id": f"rule-{index}",
                            "executable": f"tool{index}",
                            "max_risk": self.rng.choice(("low", "medium", "high")),
                        }
                        for index in range(self.rng.randrange(6))
                    ],
                }
            )
            parsed = parse_trusted_rules(document)
            identifiers = [item.rule_id for item in parsed.rules]
            self.assertEqual(len(identifiers), len(set(identifiers)))


class WindowsQuotingFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(SEED + 4)

    def test_quoting_round_trips_for_arbitrary_arguments(self):
        from tests.unit.execution.test_windows_plan import _reference_split

        for _ in range(ROUNDS):
            argv = tuple(
                noisy_text(self.rng, 12).replace("\x00", "")
                for _ in range(self.rng.randrange(1, 5))
            )
            argv = tuple(entry for entry in argv if "\n" not in entry and "\r" not in entry)
            if not argv:
                continue
            self.assertEqual(_reference_split(build_command_line(argv)), list(argv))

    def test_quote_argument_never_produces_an_unbalanced_quote(self):
        for _ in range(ROUNDS):
            quoted = quote_argument(noisy_text(self.rng, 24))
            if quoted.startswith('"'):
                self.assertTrue(quoted.endswith('"'))

    def test_error_normalization_accepts_every_integer(self):
        for _ in range(ROUNDS):
            code = self.rng.randrange(-(2**31), 2**32)
            self.assertIsInstance(normalize_start_error(code & 0xFFFF), str)
            exit_code, _ = normalize_exit_code(code)
            self.assertIsInstance(exit_code, int)


if __name__ == "__main__":
    unittest.main()
