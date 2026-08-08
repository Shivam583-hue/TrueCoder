"""Correcting a note must leave one note, never two that disagree."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from truecoder.memory import MemoryStore
from truecoder.memory.models import note_key

V1_SCHEMA = """
CREATE TABLE memory_schema (
    version INTEGER PRIMARY KEY,
    installed_at TEXT NOT NULL
);
CREATE TABLE memory_entries (
    entry_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX memory_entries_workspace
    ON memory_entries(workspace_id, created_at);
CREATE UNIQUE INDEX memory_entries_unique_note
    ON memory_entries(workspace_id, note);
INSERT INTO memory_schema VALUES (1, '2026-01-01T00:00:00Z');
PRAGMA user_version = 1;
"""


class NoteKeyTests(unittest.TestCase):
    def test_case_and_trailing_punctuation_are_ignored(self):
        for variant in ("Use tabs", "use tabs", "USE TABS.", "Use tabs!", "use tabs ;"):
            with self.subTest(variant=variant):
                self.assertEqual(note_key(variant), note_key("Use tabs"))

    def test_different_notes_keep_different_keys(self):
        self.assertNotEqual(
            note_key("The parser lives in src/parse.py"),
            note_key("The parser lives in src/parser/core.py"),
        )

    def test_interior_punctuation_is_kept(self):
        self.assertEqual(note_key("Pin v1.2.3."), "pin v1.2.3")

    def test_a_note_of_only_punctuation_still_has_a_key(self):
        self.assertTrue(note_key("..."))


class CorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.store = MemoryStore(self.root / "memory.sqlite3", "workspace_1")
        self.addCleanup(self.store.close)

    def _notes(self) -> list[str]:
        return [entry.note for entry in self.store.entries()]

    def test_replacing_leaves_only_the_new_note(self):
        self.store.remember("The parser lives in src/parse.py")

        self.store.remember(
            "The parser lives in src/parser/core.py",
            replaces="The parser lives in src/parse.py",
        )

        self.assertEqual(self._notes(), ["The parser lives in src/parser/core.py"])

    def test_recording_without_replaces_still_keeps_both(self):
        self.store.remember("The parser lives in src/parse.py")

        self.store.remember("The parser lives in src/parser/core.py")

        self.assertEqual(len(self._notes()), 2)

    def test_replacing_something_absent_still_records_the_new_note(self):
        self.store.remember("A fact", replaces="never recorded")

        self.assertEqual(self._notes(), ["A fact"])

    def test_replacing_a_note_with_itself_keeps_one_note(self):
        self.store.remember("A fact")

        self.store.remember("A fact.", replaces="A fact")

        self.assertEqual(len(self._notes()), 1)

    def test_a_near_duplicate_updates_the_wording(self):
        self.store.remember("Use tabs")

        self.store.remember("use tabs.")

        self.assertEqual(self._notes(), ["use tabs."])

    def test_case_and_punctuation_variants_never_accumulate(self):
        for variant in ("Use tabs", "use tabs", "Use tabs.", "USE TABS!"):
            self.store.remember(variant)

        self.assertEqual(len(self._notes()), 1)

    def test_forgetting_tolerates_case_and_punctuation(self):
        self.store.remember("Use tabs")

        self.assertTrue(self.store.forget_note("USE TABS."))
        self.assertEqual(self._notes(), [])

    def test_forgetting_something_genuinely_absent_still_reports_false(self):
        self.store.remember("Use tabs")

        self.assertFalse(self.store.forget_note("Use spaces"))

    def test_a_failed_replacement_leaves_memory_untouched(self):
        self.store.remember("A fact")

        with self.assertRaises(ValueError):
            self.store.remember("   ", replaces="A fact")

        self.assertEqual(self._notes(), ["A fact"])


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.path = self.root / "memory.sqlite3"

    def _seed(self, rows: list[tuple[str, str, str, str]]) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(V1_SCHEMA)
        connection.executemany(
            "INSERT INTO memory_entries VALUES (?, ?, ?, ?)",
            rows,
        )
        connection.commit()
        connection.close()

    def test_a_version_one_database_is_migrated(self):
        self._seed([("a", "workspace_1", "Keep this", "2026-01-01T00:00:01Z")])
        store = MemoryStore(self.path, "workspace_1")
        self.addCleanup(store.close)

        notes = [entry.note for entry in store.entries()]

        self.assertEqual(notes, ["Keep this"])

    def test_migration_collapses_near_duplicates_to_the_newest(self):
        self._seed(
            [
                ("a", "workspace_1", "Use tabs", "2026-01-01T00:00:01Z"),
                ("b", "workspace_1", "use tabs.", "2026-01-01T00:00:02Z"),
                ("c", "workspace_1", "Keep this", "2026-01-01T00:00:03Z"),
            ]
        )
        store = MemoryStore(self.path, "workspace_1")
        self.addCleanup(store.close)

        notes = [entry.note for entry in store.entries()]

        self.assertEqual(notes, ["use tabs.", "Keep this"])

    def test_migration_leaves_other_workspaces_alone(self):
        self._seed(
            [
                ("a", "workspace_1", "Mine", "2026-01-01T00:00:01Z"),
                ("b", "workspace_2", "Theirs", "2026-01-01T00:00:02Z"),
            ]
        )
        store = MemoryStore(self.path, "workspace_2")
        self.addCleanup(store.close)

        self.assertEqual([entry.note for entry in store.entries()], ["Theirs"])

    def test_the_version_is_recorded_after_migrating(self):
        self._seed([("a", "workspace_1", "Keep this", "2026-01-01T00:00:01Z")])
        store = MemoryStore(self.path, "workspace_1")
        self.addCleanup(store.close)
        store.open()

        connection = sqlite3.connect(self.path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()

        self.assertEqual(version, 2)

    def test_migrating_twice_is_safe(self):
        self._seed([("a", "workspace_1", "Keep this", "2026-01-01T00:00:01Z")])
        first = MemoryStore(self.path, "workspace_1")
        first.open()
        first.close()

        second = MemoryStore(self.path, "workspace_1")
        self.addCleanup(second.close)

        self.assertEqual([entry.note for entry in second.entries()], ["Keep this"])

    def test_a_migrated_store_still_accepts_writes(self):
        self._seed([("a", "workspace_1", "Use tabs", "2026-01-01T00:00:01Z")])
        store = MemoryStore(self.path, "workspace_1")
        self.addCleanup(store.close)

        store.remember("Use spaces", replaces="use tabs")

        self.assertEqual([entry.note for entry in store.entries()], ["Use spaces"])


if __name__ == "__main__":
    unittest.main()
