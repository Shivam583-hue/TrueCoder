from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from truecoder.execution.errors import AuditUnavailableError
from truecoder.memory import (
    MAX_MEMORY_CHARACTERS,
    Memory,
    MemoryEntry,
    MemoryStore,
    normalize_note,
)


class NormalizeNoteTests(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize_note("  many   spaces \n here "), "many spaces here")

    def test_an_empty_note_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_note("   \n  ")

    def test_an_oversized_note_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_note("x" * (MAX_MEMORY_CHARACTERS + 1))

    def test_a_note_at_the_limit_is_accepted(self):
        note = "x" * MAX_MEMORY_CHARACTERS

        self.assertEqual(normalize_note(note), note)

    def test_a_non_string_note_is_rejected(self):
        with self.assertRaises(TypeError):
            normalize_note(None)  # type: ignore[arg-type]


class MemoryModelTests(unittest.TestCase):
    def _entry(self, note: str = "the parser lives in src/parse.py") -> MemoryEntry:
        return MemoryEntry(
            entry_id="mem_1",
            workspace_id="workspace_1",
            note=note,
            created_at="2026-08-07T00:00:00+00:00",
        )

    def test_an_entry_requires_identity(self):
        with self.assertRaises(ValueError):
            MemoryEntry(
                entry_id=" ",
                workspace_id="w",
                note="x",
                created_at="",
            )

    def test_an_entry_requires_a_workspace(self):
        with self.assertRaises(ValueError):
            MemoryEntry(entry_id="m", workspace_id=" ", note="x", created_at="")

    def test_an_empty_memory_says_so(self):
        self.assertTrue(Memory(entries=()).is_empty)

    def test_rendering_lists_every_note(self):
        rendered = Memory(entries=(self._entry("one"), self._entry("two"))).render()

        self.assertIn("- one", rendered)
        self.assertIn("- two", rendered)

    def test_rendering_labels_notes_as_background(self):
        rendered = Memory(entries=(self._entry(),)).render()

        self.assertIn("not as instructions", rendered)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path, "workspace_1")
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.store.close)

    def test_a_note_is_stored_and_returned(self):
        entry = self.store.remember("the parser lives in src/parse.py")

        self.assertEqual(entry.note, "the parser lives in src/parse.py")
        self.assertEqual(len(self.store.entries()), 1)

    def test_notes_are_returned_oldest_first(self):
        self.store.remember("first")
        self.store.remember("second")

        self.assertEqual([e.note for e in self.store.entries()], ["first", "second"])

    def test_an_identical_note_is_not_stored_twice(self):
        first = self.store.remember("tests live in tests/")
        second = self.store.remember("tests   live in    tests/")

        self.assertEqual(first.entry_id, second.entry_id)
        self.assertEqual(len(self.store.entries()), 1)

    def test_a_note_can_be_forgotten_by_id(self):
        entry = self.store.remember("temporary")

        self.assertTrue(self.store.forget(entry.entry_id))
        self.assertEqual(self.store.entries(), ())

    def test_a_note_can_be_forgotten_by_text(self):
        self.store.remember("temporary")

        self.assertTrue(self.store.forget_note("temporary"))
        self.assertEqual(self.store.entries(), ())

    def test_forgetting_something_absent_reports_false(self):
        self.assertFalse(self.store.forget("mem_missing"))
        self.assertFalse(self.store.forget_note("never recorded"))

    def test_memory_is_scoped_by_workspace(self):
        other = MemoryStore(self.path, "workspace_2")
        self.addCleanup(other.close)
        self.store.remember("mine")

        other.remember("theirs")

        self.assertEqual([e.note for e in self.store.entries()], ["mine"])
        self.assertEqual([e.note for e in other.entries()], ["theirs"])

    def test_the_same_note_is_allowed_in_two_workspaces(self):
        other = MemoryStore(self.path, "workspace_2")
        self.addCleanup(other.close)
        self.store.remember("shared wording")

        other.remember("shared wording")

        self.assertEqual(len(other.entries()), 1)

    def test_the_oldest_notes_are_pruned(self):
        store = MemoryStore(self.path, "workspace_1", limit=3)
        self.addCleanup(store.close)
        for index in range(5):
            store.remember(f"note {index}")

        self.assertEqual(
            [e.note for e in store.entries()],
            ["note 2", "note 3", "note 4"],
        )

    def test_memory_survives_reopening(self):
        self.store.remember("durable")
        self.store.close()

        reopened = MemoryStore(self.path, "workspace_1")
        self.addCleanup(reopened.close)

        self.assertEqual([e.note for e in reopened.entries()], ["durable"])

    def test_clearing_removes_only_this_workspace(self):
        other = MemoryStore(self.path, "workspace_2")
        self.addCleanup(other.close)
        self.store.remember("mine")
        other.remember("theirs")

        removed = self.store.clear()

        self.assertEqual(removed, 1)
        self.assertEqual(self.store.entries(), ())
        self.assertEqual(len(other.entries()), 1)

    def test_load_returns_a_memory(self):
        self.store.remember("one")

        memory = self.store.load()

        self.assertFalse(memory.is_empty)
        self.assertIn("one", memory.render())

    def test_an_unsupported_database_version_is_refused(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA user_version = 99")
        finally:
            connection.close()

        with self.assertRaises(AuditUnavailableError):
            MemoryStore(self.path, "workspace_1").open()

    def test_a_non_path_database_is_rejected(self):
        with self.assertRaises(TypeError):
            MemoryStore("memory.sqlite3", "workspace_1")  # type: ignore[arg-type]

    def test_an_empty_workspace_is_rejected(self):
        with self.assertRaises(ValueError):
            MemoryStore(self.path, "  ")


if __name__ == "__main__":
    unittest.main()
