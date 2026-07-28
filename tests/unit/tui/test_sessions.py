import tempfile
import unittest
from pathlib import Path

from textual.widgets import Input, ListView

from tests.unit.tui.test_app import FakeLLMClient, make_agent
from truecoder.session import SessionManager, SQLiteSessionStore
from truecoder.tools import ToolCall, ToolResult, serialize_tool_result
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.sessions import (
    DeleteSessionScreen,
    RenameSessionScreen,
    SessionListItem,
    SessionManagerScreen,
)
from truecoder.tui.widgets import ChatMessage, ToolCallCard


class SessionManagerUITests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, root: Path) -> tuple[TrueCoderApp, SessionManager]:
        project = root / "project"
        project.mkdir()
        agent = make_agent(FakeLLMClient([]))
        manager = SessionManager(
            SQLiteSessionStore(root / "sessions.sqlite3"),
            agent.state,
            project,
        )
        return TrueCoderApp(agent, session_manager=manager), manager

    async def test_ctrl_p_lists_project_sessions_and_marks_active(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())
            manager.state.begin_turn("Saved session")
            manager.state.complete_turn("Answer")
            manager.save_completed_turns()
            manager.create_session()

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p")
                await pilot.pause()

                self.assertIsInstance(app.screen, SessionManagerScreen)
                items = list(app.screen.query(SessionListItem))
                self.assertEqual(len(items), 2)
                self.assertEqual(sum(item.active for item in items), 1)

    async def test_escape_closes_session_manager(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, _manager = self.make_app(Path(temporary_directory).resolve())

            async with app.run_test(size=(120, 40)) as pilot:
                base_screen = app.screen
                await pilot.press("ctrl+p")
                await pilot.pause()
                self.assertIsInstance(app.screen, SessionManagerScreen)

                await pilot.press("escape")
                await pilot.pause()

                self.assertIs(app.screen, base_screen)

    async def test_new_session_action_creates_and_activates_a_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())
            previous_id = manager.active_session.session_id

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p", "n")
                await pilot.pause()

                self.assertNotEqual(manager.active_session.session_id, previous_id)
                self.assertEqual(len(manager.list_sessions()), 1)

    async def test_switch_restores_the_selected_transcript(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())
            first_id = manager.active_session.session_id
            app.agent.state.begin_turn("First question")
            app.agent.state.complete_turn("First answer")
            manager.save_completed_turns()
            manager.create_session()

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p")
                session_list = app.screen.query_one(ListView)
                session_list.index = 1
                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(manager.active_session.session_id, first_id)
                self.assertEqual(
                    [
                        (message.role, message.content_text)
                        for message in app.query(ChatMessage)
                    ],
                    [
                        ("user", "First question"),
                        ("assistant", "First answer"),
                    ],
                )

    async def test_switch_restores_a_completed_write_file_card(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())
            app.agent.state.begin_turn("Create example.py")
            app.agent.state.record_tool_calls(
                (
                    ToolCall(
                        "call_write",
                        "write_file",
                        '{"path":"example.py","content":"pass"}',
                    ),
                )
            )
            app.agent.state.record_tool_result(
                "call_write",
                serialize_tool_result(
                    ToolResult.success(
                        "call_write",
                        "write_file",
                        {
                            "path": "example.py",
                            "created": True,
                            "bytes_written": 4,
                        },
                    )
                ),
            )
            app.agent.state.complete_turn("Created example.py.")
            saved_id = manager.save_completed_turns().session_id
            manager.create_session()

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p")
                session_list = app.screen.query_one(ListView)
                session_list.index = 1
                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(manager.active_session.session_id, saved_id)
                cards = list(app.query(ToolCallCard))
                self.assertEqual(len(cards), 1)
                self.assertTrue(cards[0].restored)
                self.assertEqual(cards[0].state, "completed")
                self.assertEqual(
                    str(cards[0].query_one(".tool-title").content),
                    "Wrote example.py · 4 bytes",
                )

    async def test_rename_updates_the_selected_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p", "r")
                await pilot.pause()
                self.assertIsInstance(app.screen, RenameSessionScreen)
                session_input = app.screen.query_one(Input)
                session_input.value = "Renamed session"
                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(manager.active_session.title, "Renamed session")
                self.assertTrue(manager.active_session.title_is_custom)

    async def test_delete_requires_confirmation_and_replaces_active_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app, manager = self.make_app(Path(temporary_directory).resolve())
            deleted_id = manager.active_session.session_id

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p", "d")
                await pilot.pause()
                self.assertIsInstance(app.screen, DeleteSessionScreen)
                await pilot.click("#session-delete-confirm")
                await pilot.pause()

                self.assertNotEqual(manager.active_session.session_id, deleted_id)
                self.assertEqual(len(manager.list_sessions()), 1)


if __name__ == "__main__":
    unittest.main()
