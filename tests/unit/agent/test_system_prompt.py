"""The system prompt must teach the agent how to work in a real repository."""

from __future__ import annotations

import unittest

from truecoder.agent.budget import tool_result_ceiling
from truecoder.agent.context import TiktokenTokenCounter
from truecoder.agent.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    add_plan_tool_guidance,
    add_shell_tool_guidance,
    build_system_prompt,
)

DEFAULT_MAX_INPUT_TOKENS = 64000
MAX_PROMPT_SHARE = 0.05


def _flat(text: str) -> str:
    return " ".join(text.split()).lower()


def _full_prompt() -> str:
    return add_plan_tool_guidance(add_shell_tool_guidance(build_system_prompt()))


class SystemPromptContentTests(unittest.TestCase):
    def test_it_says_to_learn_the_project_before_running_things(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("continuous integration", prompt)
        self.assertIn("packaging manifest", prompt)

    def test_it_forbids_changing_the_user_environment_to_make_a_command_work(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("never install a tool", prompt)
        self.assertIn("add a dependency", prompt)

    def test_it_names_the_two_mistakes_seen_in_practice(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("do not assume a test runner", prompt)
        self.assertIn("do not assume a bare interpreter", prompt)

    def test_it_forbids_claiming_unverified_results(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("only when you ran them and saw them pass", prompt)

    def test_it_explains_what_a_shortened_result_means(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("shortened", prompt)
        self.assertIn("never to repeat the same read", prompt)

    def test_it_discourages_repeating_a_failed_call_unchanged(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("repeating a call that just failed", prompt)

    def test_it_prefers_code_intelligence_over_text_search(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("code intelligence", prompt)

    def test_it_says_each_call_costs_the_user_a_decision(self):
        prompt = _flat(DEFAULT_SYSTEM_PROMPT)

        self.assertIn("approval", prompt)


class SystemPromptCostTests(unittest.TestCase):
    def test_the_prompt_stays_a_small_share_of_the_budget(self):
        counter = TiktokenTokenCounter("gpt-4")
        tokens = counter.count_message({"role": "system", "content": _full_prompt()})

        self.assertLess(tokens, DEFAULT_MAX_INPUT_TOKENS * MAX_PROMPT_SHARE)

    def test_a_full_default_read_fits_without_being_shortened(self):
        ceiling = tool_result_ceiling(DEFAULT_MAX_INPUT_TOKENS)
        counter = TiktokenTokenCounter("gpt-4")
        dense_file = "\n".join(
            f"    self.assertEqual(result_{n}.value, expected_{n})" for n in range(500)
        )
        tokens = counter.count_message(
            {"role": "tool", "tool_call_id": "c1", "content": dense_file}
        )

        self.assertLess(tokens, ceiling)


if __name__ == "__main__":
    unittest.main()
