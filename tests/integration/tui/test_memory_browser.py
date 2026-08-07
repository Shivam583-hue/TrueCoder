"""Integration coverage for viewing and editing what the agent remembers."""

import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.memory import MemoryStore
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.memory import MemoryAction, MemoryBrowserScreen, MemoryListItem


@asynccontextmanager
async def running(app: TrueCoderApp):
    with patch.dict(os.environ, {"MODEL": "test-model"}):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            yield pilot


async def wait_for(pilot, app, selector):
    await wait_until(
        pilot,
        lambda: bool(app.screen.query(selector)),
        description=f"{selector} to be mounted",
    )
    return app.screen.query_one(selector)


class MemoryBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            Path(self._directory.name) / "memory.sqlite3",
            "workspace_1",
        )
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.store.close)

    def _app(self, store: MemoryStore | None) -> TrueCoderApp:
        agent = Agent(
            llm_client=ScriptedLLMClient([]),
            memory_store=store,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def test_recorded_notes_are_listed(self):
        self.store.remember("the parser lives in src/parse.py")
        self.store.remember("tests run with unittest")
        app = self._app(self.store)

        async with running(app) as pilot:
            app.action_manage_memory()
            await wait_until(
                pilot,
                lambda: bool(app.screen.query(MemoryListItem)),
                description="the memory list to be mounted",
            )

            items = list(app.screen.query(MemoryListItem))
            self.assertIsInstance(app.screen, MemoryBrowserScreen)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].entry.note, "the parser lives in src/parse.py")

    async def test_an_empty_memory_says_so(self):
        app = self._app(self.store)

        async with running(app) as pilot:
            app.action_manage_memory()

            self.assertTrue(await wait_for(pilot, app, "#memory-empty"))

    async def test_the_summary_says_the_notes_are_sent_every_reply(self):
        self.store.remember("one")
        app = self._app(self.store)

        async with running(app) as pilot:
            app.action_manage_memory()
            summary = await wait_for(pilot, app, "#memory-summary")

            self.assertIn("before every reply", str(summary.render()))

    async def test_forgetting_one_note_removes_it(self):
        entry = self.store.remember("temporary")
        self.store.remember("kept")
        app = self._app(self.store)

        async with running(app):
            app._handle_memory_action(
                MemoryAction(kind="forget", entry_id=entry.entry_id)
            )

        self.assertEqual([e.note for e in self.store.entries()], ["kept"])

    async def test_clearing_removes_every_note(self):
        self.store.remember("one")
        self.store.remember("two")
        app = self._app(self.store)

        async with running(app):
            app._handle_memory_action(MemoryAction(kind="clear"))

        self.assertEqual(self.store.entries(), ())

    async def test_cancelling_changes_nothing(self):
        self.store.remember("kept")
        app = self._app(self.store)

        async with running(app):
            app._handle_memory_action(None)

        self.assertEqual([e.note for e in self.store.entries()], ["kept"])

    async def test_no_store_is_reported_rather_than_crashing(self):
        app = self._app(None)

        async with running(app):
            app.action_manage_memory()

            self.assertNotIsInstance(app.screen, MemoryBrowserScreen)

    async def test_an_unreadable_store_is_reported(self):
        self.store.remember("one")
        self.store.close()
        self.store.database_path.write_bytes(b"not a database")
        app = self._app(self.store)
        notices: list[str] = []
        app.notify = lambda message, **kwargs: notices.append(str(message))

        async with running(app):
            app.action_manage_memory()

            self.assertNotIsInstance(app.screen, MemoryBrowserScreen)
            self.assertTrue(any("could not be read" in n for n in notices))


if __name__ == "__main__":
    unittest.main()
