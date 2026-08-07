from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from truecoder.hooks.configuration import load_hooks, parse_hooks
from truecoder.hooks.models import Hook, HookConfigError, HookOutcome, HookSuite


def _config(**overrides) -> str:
    hook = {
        "name": "format",
        "event": "turn_end",
        "command": ["ruff", "format", "."],
    }
    hook.update(overrides)
    return json.dumps({"version": 1, "hooks": [hook]})


class HookModelTests(unittest.TestCase):
    def _hook(self, **overrides) -> Hook:
        payload = {
            "name": "format",
            "event": "turn_end",
            "command": ("ruff", "format", "."),
        }
        payload.update(overrides)
        return Hook(**payload)  # type: ignore[arg-type]

    def test_a_valid_hook_is_accepted(self):
        hook = self._hook()

        self.assertEqual(hook.name, "format")
        self.assertEqual(hook.display, "ruff format .")

    def test_a_hook_requires_a_name(self):
        with self.assertRaises(HookConfigError):
            self._hook(name="  ")

    def test_a_hook_requires_a_command(self):
        with self.assertRaises(HookConfigError):
            self._hook(command=())

    def test_an_empty_argument_is_rejected(self):
        with self.assertRaises(HookConfigError):
            self._hook(command=("ruff", ""))

    def test_an_unknown_event_is_rejected(self):
        with self.assertRaises(HookConfigError):
            self._hook(event="on_whatever")

    def test_an_unknown_condition_is_rejected(self):
        with self.assertRaises(HookConfigError):
            self._hook(condition="sometimes")

    def test_files_changed_requires_turn_end(self):
        with self.assertRaises(HookConfigError):
            self._hook(event="turn_start", condition="files_changed")

    def test_an_invalid_timeout_is_rejected(self):
        with self.assertRaises(HookConfigError):
            self._hook(timeout_seconds=0)
        with self.assertRaises(HookConfigError):
            self._hook(timeout_seconds=10_000)

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(HookConfigError):
            HookSuite(hooks=(self._hook(), self._hook()))

    def test_too_many_hooks_are_rejected(self):
        hooks = tuple(self._hook(name=f"hook {index}") for index in range(11))

        with self.assertRaises(HookConfigError):
            HookSuite(hooks=hooks)

    def test_hooks_are_selected_by_event(self):
        suite = HookSuite(
            hooks=(
                self._hook(name="start", event="turn_start"),
                self._hook(name="end", event="turn_end"),
            )
        )

        self.assertEqual([h.name for h in suite.for_event("turn_start")], ["start"])
        self.assertEqual([h.name for h in suite.for_event("turn_end")], ["end"])

    def test_a_files_changed_hook_is_skipped_when_nothing_changed(self):
        suite = HookSuite(hooks=(self._hook(condition="files_changed"),))

        self.assertEqual(suite.for_event("turn_end"), ())
        self.assertEqual(len(suite.for_event("turn_end", files_changed=True)), 1)

    def test_an_unavailable_suite_selects_nothing(self):
        suite = HookSuite(
            hooks=(self._hook(),),
            unavailable_reason="broken",
        )

        self.assertFalse(suite.available)
        self.assertEqual(suite.for_event("turn_end"), ())

    def test_an_outcome_describes_itself(self):
        hook = self._hook()

        self.assertTrue(HookOutcome(hook, "completed", 0).ok)
        self.assertIn("exited 1", HookOutcome(hook, "completed", 1).summary)
        self.assertIn("timed_out", HookOutcome(hook, "timed_out").summary)


class ParseHooksTests(unittest.TestCase):
    def test_a_valid_configuration_is_parsed(self):
        suite = parse_hooks(_config())

        self.assertTrue(suite.available)
        self.assertEqual(suite.hooks[0].command, ("ruff", "format", "."))
        self.assertEqual(suite.hooks[0].condition, "always")

    def test_a_condition_is_read_from_when(self):
        suite = parse_hooks(_config(when="files_changed"))

        self.assertEqual(suite.hooks[0].condition, "files_changed")

    def test_an_empty_configuration_is_valid(self):
        suite = parse_hooks(json.dumps({"version": 1, "hooks": []}))

        self.assertTrue(suite.available)
        self.assertEqual(suite.hooks, ())

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(json.dumps({"version": 2, "hooks": []}))

    def test_a_missing_version_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(json.dumps({"hooks": []}))

    def test_unknown_root_fields_are_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(json.dumps({"version": 1, "hooks": [], "extra": 1}))

    def test_unknown_hook_fields_are_refused(self):
        with self.assertRaises(HookConfigError) as caught:
            parse_hooks(_config(shell=True))

        self.assertIn("unknown hook fields", str(caught.exception))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks("{not json")

    def test_an_oversized_configuration_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(" " * (64 * 1024 + 1))

    def test_a_non_object_configuration_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(json.dumps([1, 2]))

    def test_a_non_list_command_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(_config(command="ruff format ."))

    def test_a_non_numeric_timeout_is_refused(self):
        with self.assertRaises(HookConfigError):
            parse_hooks(_config(timeout_seconds="fast"))


class LoadHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "hooks.json"
        self.addCleanup(self._directory.cleanup)

    def test_a_missing_file_means_no_hooks(self):
        suite = load_hooks(self.path)

        self.assertTrue(suite.available)
        self.assertEqual(suite.hooks, ())

    def test_a_valid_file_is_loaded(self):
        self.path.write_text(_config(), encoding="utf-8")

        self.assertEqual(len(load_hooks(self.path).hooks), 1)

    def test_a_broken_file_disables_hooks_with_a_reason(self):
        self.path.write_text("{not json", encoding="utf-8")

        suite = load_hooks(self.path)

        self.assertFalse(suite.available)
        assert suite.unavailable_reason is not None
        self.assertIn("not valid JSON", suite.unavailable_reason)

    def test_a_broken_file_never_raises(self):
        self.path.write_text(_config(event="whenever"), encoding="utf-8")

        suite = load_hooks(self.path)

        self.assertFalse(suite.available)
        self.assertEqual(suite.for_event("turn_end"), ())


if __name__ == "__main__":
    unittest.main()
