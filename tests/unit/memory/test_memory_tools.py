"""The tools must expose correction, and say what exists when forget misses."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from truecoder.memory import MemoryStore
from truecoder.tools.builtin.memory import ForgetTool, RememberTool, memory_tools


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.store = MemoryStore(self.root / "memory.sqlite3", "workspace_1")
        self.addCleanup(self.store.close)
        self.remember = RememberTool(self.store)
        self.forget = ForgetTool(self.store)

    async def _remember(self, **arguments):
        return await self.remember.run(
            self.remember.parse_arguments(json.dumps(arguments))
        )

    async def _forget(self, note: str):
        return await self.forget.run(
            self.forget.parse_arguments(json.dumps({"note": note}))
        )

    async def test_a_note_is_recorded(self):
        result = await self._remember(note="Tests run with unittest")

        self.assertEqual(result["note"], "Tests run with unittest")
        self.assertEqual(result["stored"], 1)
        self.assertNotIn("replaced", result)

    async def test_a_correction_replaces_rather_than_accumulates(self):
        await self._remember(note="The parser lives in src/parse.py")

        result = await self._remember(
            note="The parser lives in src/parser/core.py",
            replaces="The parser lives in src/parse.py",
        )

        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["replaced"], "The parser lives in src/parse.py")

    async def test_replacing_reports_nothing_when_there_was_nothing(self):
        result = await self._remember(note="A fact", replaces="never recorded")

        self.assertNotIn("replaced", result)
        self.assertEqual(result["stored"], 1)

    async def test_replaces_is_optional(self):
        definition = self.remember.definition()

        self.assertEqual(definition.parameters["required"], ["note"])
        self.assertIn("replaces", definition.parameters["properties"])

    async def test_the_description_tells_the_model_how_to_correct(self):
        self.assertIn("replaces", self.remember.description)

    async def test_forgetting_a_known_note_reports_no_alternatives(self):
        await self._remember(note="Use tabs")

        result = await self._forget("Use tabs")

        self.assertTrue(result["removed"])
        self.assertNotIn("available", result)

    async def test_forgetting_tolerates_case_and_punctuation(self):
        await self._remember(note="Use tabs")

        result = await self._forget("USE TABS.")

        self.assertTrue(result["removed"])

    async def test_a_missed_forget_lists_what_is_actually_stored(self):
        await self._remember(note="Use tabs")
        await self._remember(note="Tests run with unittest")

        result = await self._forget("something never recorded")

        self.assertFalse(result["removed"])
        self.assertEqual(
            sorted(result["available"]),
            ["Tests run with unittest", "Use tabs"],
        )

    async def test_a_missed_forget_on_empty_memory_lists_nothing(self):
        result = await self._forget("anything")

        self.assertFalse(result["removed"])
        self.assertEqual(result["available"], [])

    async def test_both_tools_are_offered_together(self):
        names = sorted(tool.name for tool in memory_tools(self.store))

        self.assertEqual(names, ["forget", "remember"])


if __name__ == "__main__":
    unittest.main()
