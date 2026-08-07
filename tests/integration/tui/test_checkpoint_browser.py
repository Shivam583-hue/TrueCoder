"""Integration coverage for browsing and restoring workspace checkpoints."""

import os
import subprocess
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.checkpoint import CheckpointService, GitWorkspace
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.checkpoints import (
    CheckpointBrowserScreen,
    CheckpointListItem,
    RestoreCheckpointScreen,
    describe_removals,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _reply(text: str = "done"):
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
    ]


@asynccontextmanager
async def running(app: TrueCoderApp):
    with patch.dict(os.environ, {"MODEL": "test-model"}):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            yield pilot


async def wait_for(pilot, app, selector: str):
    await wait_until(
        pilot,
        lambda: bool(app.screen.query(selector)),
        description=f"{selector} to be mounted",
    )
    return app.screen.query_one(selector)


class DescribeRemovalsTests(unittest.TestCase):
    def test_no_removals_is_stated_plainly(self):
        self.assertIn("No tracked files", describe_removals(()))

    def test_removals_are_listed(self):
        described = describe_removals(("a.py", "b.py"))

        self.assertIn("2 tracked file(s) will be removed", described)
        self.assertIn("a.py", described)

    def test_a_long_list_is_summarised(self):
        described = describe_removals(tuple(f"f{index}.py" for index in range(20)))

        self.assertIn("and 12 more", described)


class CheckpointBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _initialise(self) -> None:
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"original\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def _app(self, service: CheckpointService | None) -> TrueCoderApp:
        agent = Agent(
            llm_client=ScriptedLLMClient([_reply()]),
            project_root=self.root,
            checkpoints=service,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def test_the_browser_lists_captured_checkpoints(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        await service.capture("first turn")
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_manage_checkpoints()
            await wait_until(
                pilot,
                lambda: bool(app.screen.query(CheckpointListItem)),
                description="the checkpoint list to be mounted",
            )

            items = list(app.screen.query(CheckpointListItem))
            self.assertIsInstance(app.screen, CheckpointBrowserScreen)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].checkpoint.label, "first turn")

    async def test_a_non_repository_explains_itself(self):
        service = CheckpointService(GitWorkspace(self.root))
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_manage_checkpoints()
            reason = await wait_for(pilot, app, "#checkpoint-unavailable")

            self.assertIn("not a git repository", str(reason.render()))

    async def test_an_empty_history_says_so(self):
        self._initialise()
        app = self._app(CheckpointService(GitWorkspace(self.root)))

        async with running(app) as pilot:
            await app.action_manage_checkpoints()

            self.assertTrue(await wait_for(pilot, app, "#checkpoint-empty"))

    async def test_restoring_returns_the_workspace(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        checkpoint = await service.capture("before the change")
        assert checkpoint is not None
        (self.root / "app.py").write_bytes(b"the agent changed this\n")
        app = self._app(service)

        async with running(app):
            await app._restore_checkpoint(checkpoint.checkpoint_id)

        self.assertEqual((self.root / "app.py").read_bytes(), b"original\n")

    async def test_a_failed_restore_is_reported_not_raised(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        app = self._app(service)

        async with running(app):
            await app._restore_checkpoint("does-not-exist")

            self.assertTrue(app.is_running)

    async def test_the_confirmation_names_what_will_be_removed(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        checkpoint = await service.capture("before")
        assert checkpoint is not None
        (self.root / "added.py").write_bytes(b"new\n")
        _git(self.root, "add", "added.py")
        app = self._app(service)

        async with running(app) as pilot:
            await app._handle_checkpoint_choice(checkpoint.checkpoint_id)
            removals = await wait_for(pilot, app, "#checkpoint-removals")

            self.assertIsInstance(app.screen, RestoreCheckpointScreen)
            self.assertIn("added.py", str(removals.render()))

    async def test_the_confirmation_promises_an_undo(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        checkpoint = await service.capture("before")
        assert checkpoint is not None
        app = self._app(service)

        async with running(app) as pilot:
            await app._handle_checkpoint_choice(checkpoint.checkpoint_id)
            safety = await wait_for(pilot, app, "#checkpoint-safety")

            self.assertIn("can be undone", str(safety.render()))

    async def test_no_service_is_reported_rather_than_crashing(self):
        app = self._app(None)

        async with running(app):
            await app.action_manage_checkpoints()

            self.assertNotIsInstance(app.screen, CheckpointBrowserScreen)


if __name__ == "__main__":
    unittest.main()
