"""A slash command is handled here, never sent to the model as a prompt."""

from __future__ import annotations

import unittest

from truecoder.tui.commands import (
    COMMANDS,
    SlashCommand,
    help_text,
    looks_like_command,
    parse_command,
    unknown_command_message,
)


class RecogniseTests(unittest.TestCase):
    def test_a_leading_slash_is_a_command(self):
        for text in ("/models", "  /help  ", "/models refresh"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_command(text))

    def test_ordinary_prompts_are_not_commands(self):
        for text in ("fix the bug", "what does a/b do?", "", "   "):
            with self.subTest(text=text):
                self.assertFalse(looks_like_command(text))

    def test_a_multiline_prompt_is_never_a_command(self):
        self.assertFalse(looks_like_command("/models\nand also do this"))


class ParseTests(unittest.TestCase):
    def test_a_known_command_parses(self):
        parsed = parse_command("/models")

        assert parsed is not None
        self.assertEqual(parsed.name, "models")
        self.assertEqual(parsed.argument, "")

    def test_an_argument_is_carried(self):
        parsed = parse_command("/models refresh")

        assert parsed is not None
        self.assertEqual(parsed.argument, "refresh")

    def test_case_is_ignored(self):
        parsed = parse_command("/MODELS")

        assert parsed is not None
        self.assertEqual(parsed.name, "models")

    def test_an_unknown_command_does_not_parse(self):
        self.assertIsNone(parse_command("/nonsense"))

    def test_a_bare_slash_does_not_parse(self):
        self.assertIsNone(parse_command("/"))

    def test_a_plain_prompt_does_not_parse(self):
        self.assertIsNone(parse_command("tell me about /models"))


class MessageTests(unittest.TestCase):
    def test_an_unknown_command_lists_the_known_ones(self):
        message = unknown_command_message("/nope")

        self.assertIn("/nope", message)
        for command in COMMANDS:
            self.assertIn(command.invocation, message)

    def test_help_lists_every_command_with_a_summary(self):
        text = help_text()

        for command in COMMANDS:
            self.assertIn(command.invocation, text)
            self.assertIn(command.summary, text)


class RegistryTests(unittest.TestCase):
    def test_names_are_unique(self):
        names = [command.name for command in COMMANDS]

        self.assertEqual(len(names), len(set(names)))

    def test_a_command_name_must_be_one_word(self):
        with self.assertRaises(ValueError):
            SlashCommand("two words", "does a thing")

    def test_a_command_needs_a_summary(self):
        with self.assertRaises(ValueError):
            SlashCommand("thing", "   ")


if __name__ == "__main__":
    unittest.main()
