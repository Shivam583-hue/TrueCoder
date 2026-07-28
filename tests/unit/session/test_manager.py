import tempfile
import unittest
from pathlib import Path

from truecoder.agent import AgentState
from truecoder.session import SessionManager, SQLiteSessionStore


def complete_turn(state: AgentState, prompt: str, answer: str = "Answer") -> None:
    state.begin_turn(prompt)
    state.complete_turn(answer)


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        self.store = SQLiteSessionStore(root / "sessions.sqlite3")
        self.state = AgentState()
        self.manager = SessionManager(self.store, self.state, self.project)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary_directory.cleanup()

    def test_immediately_creates_an_active_session(self):
        active = self.manager.active_session

        self.assertEqual(active.title, "New session")
        self.assertEqual(self.manager.list_sessions(), (active,))

    def test_saves_completed_turns_and_derives_first_title(self):
        complete_turn(self.state, "  Explain   the architecture  ")

        saved = self.manager.save_completed_turns()

        self.assertEqual(saved.title, "Explain the architecture")
        self.assertFalse(saved.title_is_custom)
        self.assertEqual(saved.turn_count, 1)

    def test_create_session_deletes_the_previous_empty_session(self):
        previous_id = self.manager.active_session.session_id

        created = self.manager.create_session()

        self.assertNotEqual(created.session_id, previous_id)
        self.assertEqual(self.manager.list_sessions(), (created,))

    def test_create_session_preserves_a_previous_session_with_turns(self):
        complete_turn(self.state, "Keep this")
        previous = self.manager.save_completed_turns()

        created = self.manager.create_session()

        self.assertEqual(
            {session.session_id for session in self.manager.list_sessions()},
            {previous.session_id, created.session_id},
        )

    def test_custom_title_survives_later_saves(self):
        active_id = self.manager.active_session.session_id
        renamed = self.manager.rename_session(active_id, "  My   work  ")
        complete_turn(self.state, "Automatic title")

        saved = self.manager.save_completed_turns()

        self.assertEqual(renamed.title, "My work")
        self.assertEqual(saved.title, "My work")
        self.assertTrue(saved.title_is_custom)

    def test_switch_replaces_state_with_saved_turns(self):
        first_id = self.manager.active_session.session_id
        complete_turn(self.state, "First")
        self.manager.save_completed_turns()
        second = self.manager.create_session()
        complete_turn(self.state, "Second")
        self.manager.save_completed_turns()

        record = self.manager.switch_session(first_id)

        self.assertEqual(self.manager.active_session.session_id, first_id)
        self.assertEqual(self.state.completed_turns, [list(record.completed_turns[0])])
        self.assertNotEqual(second.session_id, first_id)

    def test_switch_deletes_the_empty_session_being_left(self):
        complete_turn(self.state, "Saved")
        saved = self.manager.save_completed_turns()
        empty = self.manager.create_session()

        self.manager.switch_session(saved.session_id)

        self.assertEqual(self.manager.list_sessions(), (saved,))
        self.assertNotEqual(empty.session_id, saved.session_id)

    def test_delete_inactive_preserves_active_and_delete_active_replaces_it(self):
        first_id = self.manager.active_session.session_id
        complete_turn(self.state, "First")
        self.manager.save_completed_turns()
        second = self.manager.create_session()

        self.manager.delete_session(first_id)
        self.assertEqual(self.manager.active_session.session_id, second.session_id)

        self.manager.delete_session(second.session_id)
        self.assertNotEqual(self.manager.active_session.session_id, second.session_id)
        self.assertEqual(self.state.completed_turns, [])

    def test_validates_renamed_titles(self):
        active_id = self.manager.active_session.session_id

        for title in ("", " \n "):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.manager.rename_session(active_id, title)

        renamed = self.manager.rename_session(active_id, "x" * 150)
        self.assertEqual(len(renamed.title), 120)

    def test_close_deletes_the_active_empty_session(self):
        self.manager.close()

        store = SQLiteSessionStore(self.store.database_path)
        try:
            self.assertEqual(store.list_sessions(self.project), ())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
