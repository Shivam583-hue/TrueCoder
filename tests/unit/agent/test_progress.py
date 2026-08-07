import unittest

from truecoder.agent.progress import (
    CHANGING_RESULT_THRESHOLD,
    IDENTICAL_RESULT_THRESHOLD,
    IterationSignature,
    ProgressMonitor,
    canonical_call,
    digest,
)
from truecoder.tools import ToolCall


def _call(name: str = "read_file", arguments: str = '{"path": "a.py"}') -> ToolCall:
    return ToolCall(call_id="call_1", name=name, arguments_json=arguments)


class CanonicalCallTests(unittest.TestCase):
    def test_the_call_id_is_not_part_of_the_identity(self):
        first = ToolCall("call_1", "read_file", '{"path": "a.py"}')
        second = ToolCall("call_2", "read_file", '{"path": "a.py"}')

        self.assertEqual(canonical_call(first), canonical_call(second))

    def test_key_order_does_not_change_the_identity(self):
        first = ToolCall("c", "edit_file", '{"a": 1, "b": 2}')
        second = ToolCall("c", "edit_file", '{"b": 2, "a": 1}')

        self.assertEqual(canonical_call(first), canonical_call(second))

    def test_whitespace_does_not_change_the_identity(self):
        first = ToolCall("c", "grep", '{"pattern":"x"}')
        second = ToolCall("c", "grep", '{ "pattern" : "x" }')

        self.assertEqual(canonical_call(first), canonical_call(second))

    def test_different_arguments_are_different_identities(self):
        self.assertNotEqual(
            canonical_call(_call(arguments='{"path": "a.py"}')),
            canonical_call(_call(arguments='{"path": "b.py"}')),
        )

    def test_different_tools_are_different_identities(self):
        self.assertNotEqual(canonical_call(_call("read_file")), canonical_call(_call("grep")))

    def test_unparseable_arguments_still_yield_an_identity(self):
        self.assertIn("read_file", canonical_call(_call(arguments="not json")))


class SignatureTests(unittest.TestCase):
    def test_a_signature_describes_its_tools(self):
        signature = IterationSignature.create([_call("grep"), _call("read_file")], [])

        self.assertEqual(signature.described, "grep, read_file")

    def test_repeated_tool_names_are_described_once(self):
        signature = IterationSignature.create([_call("grep"), _call("grep")], [])

        self.assertEqual(signature.described, "grep")

    def test_results_are_stored_as_digests(self):
        signature = IterationSignature.create([_call()], ["output"])

        self.assertEqual(signature.results, (digest("output"),))


class ProgressMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = ProgressMonitor()

    def _record(self, times: int, result: str = "same") -> str | None:
        notice = None
        for _ in range(times):
            notice = self.monitor.record([_call()], [result])
        return notice

    def test_a_single_call_is_not_a_stall(self):
        self.assertIsNone(self._record(1))

    def test_two_identical_calls_are_not_yet_a_stall(self):
        self.assertIsNone(self._record(2))

    def test_the_third_identical_call_and_result_stalls(self):
        notice = self._record(IDENTICAL_RESULT_THRESHOLD)

        assert notice is not None
        self.assertIn("read_file", notice)
        self.assertIn("identical", notice)
        self.assertIn("Answer now", notice)

    def test_the_notice_reports_how_many_repeats_happened(self):
        self._record(IDENTICAL_RESULT_THRESHOLD)

        self.assertEqual(self.monitor.call_repeats, IDENTICAL_RESULT_THRESHOLD)

    def test_different_arguments_reset_the_count(self):
        self.monitor.record([_call(arguments='{"path": "a.py"}')], ["x"])
        self.monitor.record([_call(arguments='{"path": "a.py"}')], ["x"])
        notice = self.monitor.record([_call(arguments='{"path": "b.py"}')], ["x"])

        self.assertIsNone(notice)
        self.assertEqual(self.monitor.call_repeats, 1)

    def test_a_changing_result_is_tolerated_for_longer(self):
        notices = [
            self.monitor.record([_call()], [f"attempt {index}"])
            for index in range(IDENTICAL_RESULT_THRESHOLD)
        ]

        self.assertEqual(notices, [None] * IDENTICAL_RESULT_THRESHOLD)

    def test_a_changing_result_still_stalls_eventually(self):
        notice = None
        for index in range(CHANGING_RESULT_THRESHOLD):
            notice = self.monitor.record([_call()], [f"attempt {index}"])

        assert notice is not None
        self.assertIn("without reaching an answer", notice)

    def test_an_empty_batch_resets_the_monitor(self):
        self._record(2)

        self.assertIsNone(self.monitor.record([], []))
        self.assertEqual(self.monitor.call_repeats, 0)

    def test_resetting_clears_the_history(self):
        self._record(2)

        self.monitor.reset()

        self.assertIsNone(self._record(2))

    def test_a_batch_of_several_calls_is_compared_as_a_whole(self):
        batch = [_call("grep"), _call("read_file")]
        for _ in range(IDENTICAL_RESULT_THRESHOLD - 1):
            self.monitor.record(batch, ["a", "b"])

        notice = self.monitor.record(batch, ["a", "b"])

        assert notice is not None
        self.assertIn("grep, read_file", notice)

    def test_a_reordered_batch_is_a_different_signature(self):
        self.monitor.record([_call("grep"), _call("read_file")], ["a", "b"])
        self.monitor.record([_call("read_file"), _call("grep")], ["b", "a"])

        self.assertEqual(self.monitor.call_repeats, 1)

    def test_invalid_thresholds_are_rejected(self):
        with self.assertRaises(ValueError):
            ProgressMonitor(identical_threshold=1)
        with self.assertRaises(ValueError):
            ProgressMonitor(identical_threshold=5, changing_threshold=4)

    def test_the_thresholds_are_configurable(self):
        monitor = ProgressMonitor(identical_threshold=2, changing_threshold=2)

        monitor.record([_call()], ["x"])

        self.assertIsNotNone(monitor.record([_call()], ["x"]))


if __name__ == "__main__":
    unittest.main()
