"""Integration coverage for the SQLite session adapter."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from truecoder.agent.messages import ModelMessage
from truecoder.session import (
    SessionFormatError,
    SessionNotFoundError,
    SessionStorageError,
    SQLiteSessionStore,
)


def plain_turn(prompt: str = "Question") -> list[ModelMessage]:
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "Answer"},
    ]


class SQLiteSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.database = self.root / "data" / "sessions.sqlite3"
        self.store = SQLiteSessionStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_creates_lists_and_persists_sessions(self):
        created = self.store.create_session(self.project)

        self.assertEqual(created.title, "New session")
        self.assertEqual(created.turn_count, 0)
        self.assertEqual(self.store.list_sessions(self.project), (created,))

        self.store.close()
        self.store = SQLiteSessionStore(self.database)
        loaded = self.store.load_session(self.project, created.session_id)
        self.assertEqual(loaded.summary, created)
        self.assertEqual(loaded.completed_turns, ())

    def test_scopes_sessions_to_project(self):
        other_project = self.root / "other"
        other_project.mkdir()
        created = self.store.create_session(self.project)

        self.assertEqual(self.store.list_sessions(other_project), ())
        with self.assertRaises(SessionNotFoundError):
            self.store.load_session(other_project, created.session_id)

    def test_saves_only_missing_turns_and_round_trips_them(self):
        created = self.store.create_session(self.project)
        turns = [plain_turn("First"), plain_turn("Second")]

        first = self.store.save_completed_turns(
            self.project,
            created.session_id,
            turns[:1],
        )
        second = self.store.save_completed_turns(
            self.project,
            created.session_id,
            turns,
        )
        repeated = self.store.save_completed_turns(
            self.project,
            created.session_id,
            turns,
        )

        self.assertEqual(first.turn_count, 1)
        self.assertEqual(second.turn_count, 2)
        self.assertEqual(repeated, second)
        loaded = self.store.load_session(self.project, created.session_id)
        self.assertEqual(
            [list(turn) for turn in loaded.completed_turns],
            turns,
        )

    def test_rejects_state_older_than_stored_session(self):
        created = self.store.create_session(self.project)
        self.store.save_completed_turns(
            self.project,
            created.session_id,
            [plain_turn()],
        )

        with self.assertRaises(SessionStorageError):
            self.store.save_completed_turns(
                self.project,
                created.session_id,
                [],
            )

    def test_renames_and_deletes_with_cascading_turns(self):
        created = self.store.create_session(self.project)
        self.store.save_completed_turns(
            self.project,
            created.session_id,
            [plain_turn()],
        )

        renamed = self.store.rename_session(
            self.project,
            created.session_id,
            "Renamed",
        )
        self.assertEqual(renamed.title, "Renamed")
        self.assertTrue(renamed.title_is_custom)

        self.store.delete_session(self.project, created.session_id)
        self.assertEqual(self.store.list_sessions(self.project), ())
        with self.assertRaises(SessionNotFoundError):
            self.store.load_session(self.project, created.session_id)

    def test_reports_corrupt_turn_data(self):
        created = self.store.create_session(self.project)
        with self.store._connection:
            self.store._connection.execute(
                """
                INSERT INTO turns (session_id, turn_index, messages_json)
                VALUES (?, 0, ?)
                """,
                (created.session_id, "not-json"),
            )

        with self.assertRaises(SessionFormatError):
            self.store.load_session(self.project, created.session_id)

    def test_rejects_unsupported_database_version(self):
        self.store.close()
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA user_version = 99")
        connection.close()

        with self.assertRaises(SessionStorageError):
            SQLiteSessionStore(self.database)


if __name__ == "__main__":
    unittest.main()
