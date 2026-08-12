"""A slash command is handled here, never sent to the model as a prompt."""

from __future__ import annotations

import unittest

from truecoder.tui.commands import (
    COMMANDS,
    SPELLINGS,
    SlashCommand,
    command_prefix,
    completion,
    help_text,
    looks_like_command,
    matching_commands,
    parse_command,
    unknown_command_message,
)


def _names(text: str) -> list[str]:
    return [match.spelling for match in matching_commands(text)]


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
        listed = set(message.partition("Try ")[2].removesuffix(".").split(", "))

        self.assertIn("/nope", message)
        for command in COMMANDS:
            if command.visible:
                self.assertIn(command.invocation, listed)
            else:
                self.assertNotIn(command.invocation, listed)

    def test_help_lists_every_command_with_a_summary(self):
        text = help_text()
        invocations = {line.split()[0] for line in text.splitlines()}

        for command in COMMANDS:
            if command.visible:
                self.assertIn(command.invocation, invocations)
                self.assertIn(command.summary, text)
            else:
                self.assertNotIn(command.invocation, invocations)


class FilterTests(unittest.TestCase):
    def test_a_bare_slash_offers_everything(self):
        self.assertEqual(_names("/"), list(SPELLINGS))

    def test_a_letter_narrows_to_that_letter(self):
        self.assertEqual(_names("/q"), ["quit"])
        self.assertEqual(_names("/e"), ["exit"])
        self.assertEqual(_names("/m"), ["models"])
        self.assertEqual(_names("/l"), ["login", "logout"])

    def test_narrowing_continues_as_more_is_typed(self):
        self.assertEqual(_names("/mo"), ["models"])
        self.assertEqual(_names("/mod"), ["models"])
        self.assertEqual(_names("/model"), ["models"])
        self.assertEqual(_names("/models"), ["models"])

    def test_case_is_ignored_while_filtering(self):
        self.assertEqual(_names("/QU"), ["quit"])

    def test_nothing_matches_an_unknown_prefix(self):
        self.assertEqual(_names("/zzz"), [])

    def test_an_ordinary_prompt_offers_nothing(self):
        for text in ("fix the bug", "", "what does a/b do?"):
            with self.subTest(text=text):
                self.assertEqual(_names(text), [])

    def test_the_menu_closes_once_an_argument_is_being_typed(self):
        self.assertEqual(_names("/models "), [])
        self.assertEqual(_names("/models refresh"), [])

    def test_a_multiline_prompt_offers_nothing(self):
        self.assertEqual(_names("/models\nmore"), [])

    def test_the_typed_prefix_is_reported_without_the_slash(self):
        self.assertEqual(command_prefix("/mo"), "mo")
        self.assertEqual(command_prefix("/"), "")
        self.assertIsNone(command_prefix("hello"))


class CompletionTests(unittest.TestCase):
    def test_a_single_match_completes_fully(self):
        self.assertEqual(completion("/q"), "/quit")
        self.assertEqual(completion("/h"), "/help")

    def test_several_matches_complete_to_what_they_share(self):
        self.assertEqual(completion("/m"), "/models")
        self.assertEqual(completion("/l"), "/log")

    def test_nothing_is_added_when_the_shared_part_is_typed(self):
        self.assertIsNone(completion("/models"))
        self.assertIsNone(completion("/log"))

    def test_a_bare_slash_shares_nothing_to_add(self):
        self.assertIsNone(completion("/"))

    def test_an_unknown_prefix_completes_to_nothing(self):
        self.assertIsNone(completion("/zzz"))

    def test_an_ordinary_prompt_completes_to_nothing(self):
        self.assertIsNone(completion("write a test"))

    def test_a_completed_command_is_parseable(self):
        completed = completion("/q")

        assert completed is not None
        parsed = parse_command(completed)
        assert parsed is not None
        self.assertEqual(parsed.name, "quit")


class AliasTests(unittest.TestCase):
    def test_an_alias_resolves_to_the_command_it_names(self):
        parsed = parse_command("/exit")

        assert parsed is not None
        self.assertEqual(parsed.name, "quit")

    def test_an_alias_is_offered_like_any_other_spelling(self):
        self.assertIn("exit", _names("/"))
        self.assertEqual(_names("/ex"), ["exit"])

    def test_an_alias_completes_to_itself(self):
        self.assertEqual(completion("/ex"), "/exit")

    def test_an_alias_carries_an_argument_like_a_name(self):
        parsed = parse_command("/EXIT  now")

        assert parsed is not None
        self.assertEqual((parsed.name, parsed.argument), ("quit", "now"))

    def test_help_lists_every_spelling(self):
        text = help_text()

        for spelling in SPELLINGS:
            self.assertIn(f"/{spelling}", text)

    def test_old_model_commands_remain_parseable_but_are_not_advertised(self):
        current = parse_command("/model")
        connect = parse_command("/connect")

        assert current is not None
        assert connect is not None
        self.assertEqual(current.name, "model")
        self.assertEqual(connect.name, "connect")
        self.assertNotIn("model", SPELLINGS)
        self.assertNotIn("connect", SPELLINGS)

    def test_an_alias_may_not_collide_with_another_spelling(self):
        with self.assertRaises(ValueError):
            SlashCommand("thing", "does a thing", aliases=("thing",))

    def test_an_alias_must_be_one_word(self):
        with self.assertRaises(ValueError):
            SlashCommand("thing", "does a thing", aliases=("two words",))


class RegistryTests(unittest.TestCase):
    def test_names_are_unique(self):
        names = [command.name for command in COMMANDS]

        self.assertEqual(len(names), len(set(names)))

    def test_every_spelling_across_commands_is_unique(self):
        spellings = [
            spelling for command in COMMANDS for spelling in command.names
        ]

        self.assertEqual(len(spellings), len(set(spellings)))

    def test_a_command_name_must_be_one_word(self):
        with self.assertRaises(ValueError):
            SlashCommand("two words", "does a thing")

    def test_a_command_needs_a_summary(self):
        with self.assertRaises(ValueError):
            SlashCommand("thing", "   ")


if __name__ == "__main__":
    unittest.main()
