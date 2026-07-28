import json
import unittest

from truecoder.agent.messages import ModelMessage
from truecoder.session.codec import decode_turn, encode_turn
from truecoder.session.models import SessionFormatError


def tool_turn() -> list[ModelMessage]:
    return [
        {"role": "user", "content": "Read a.py"},
        {
            "role": "assistant",
            "content": "I'll read it.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"a.py"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"output":"x"}'},
        {"role": "assistant", "content": "Done."},
    ]


class SessionCodecTests(unittest.TestCase):
    def test_round_trips_plain_and_tool_turns(self):
        plain: list[ModelMessage] = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]

        for turn in (plain, tool_turn()):
            with self.subTest(turn=turn):
                self.assertEqual(list(decode_turn(encode_turn(turn))), turn)

    def test_rejects_malformed_and_unsupported_payloads(self):
        invalid_payloads = (
            "not-json",
            "[]",
            json.dumps({"version": 2, "messages": []}),
            json.dumps({"version": 1, "messages": "wrong"}),
            json.dumps(
                {
                    "version": 1,
                    "messages": [{"role": "user", "content": "unfinished"}],
                }
            ),
        )

        for payload in invalid_payloads:
            with (
                self.subTest(payload=payload),
                self.assertRaises(SessionFormatError),
            ):
                decode_turn(payload)

    def test_rejects_unresolved_or_out_of_order_tool_results(self):
        unresolved = tool_turn()
        unresolved.pop(2)
        out_of_order = tool_turn()
        out_of_order[2]["tool_call_id"] = "other"

        for turn in (unresolved, out_of_order):
            with self.subTest(turn=turn), self.assertRaises(SessionFormatError):
                encode_turn(turn)


if __name__ == "__main__":
    unittest.main()
