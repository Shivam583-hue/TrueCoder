"""Integration coverage for reviewing what a turn changed on disk."""

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
from truecoder.checkpoint import CheckpointService, GitWorkspace, WorkspaceChanges
from truecoder.checkpoint.changes import FileChange
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.changes import WorkspaceChangesScreen, render_changes


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _run(root: Path, *arguments: str) -> None:
    subprocess.run(list(arguments), cwd=root, check=True, capture_output=True)


def _reply(text: str = "done"):
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
    ]


class MutatingClient(ScriptedLLMClient):
    def __init__(self, batches, target: Path, content: bytes) -> None:
        super().__init__(batches)
        self.target = target
        self.content = content

    async def chat_completion(self, messages, stream=True, tools=None):
        self.target.write_bytes(self.content)
        async for event in super().chat_completion(messages, stream, tools):
            yield event


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


class RenderChangesTests(unittest.TestCase):
    def test_an_empty_change_set_says_nothing_changed(self):
        rendered = render_changes(
            WorkspaceChanges(label="fix the parser", changes=())
        ).plain

        self.assertIn("Nothing changed", rendered)
        self.assertIn("fix the parser", rendered)

    def test_a_created_file_is_listed_as_added(self):
        from truecoder.mutation import build_file_diff

        rendered = render_changes(
            WorkspaceChanges(
                label="turn",
                changes=(
                    FileChange(
                        path="scratch.md",
                        kind="added",
                        diff=build_file_diff("scratch.md", "", "one\n", kind="create"),
                    ),
                ),
            )
        ).plain

        self.assertIn("scratch.md", rendered)
        self.assertIn("added", rendered)

    def test_a_binary_change_is_named_without_a_diff(self):
        rendered = render_changes(
            WorkspaceChanges(
                label="turn",
                changes=(FileChange(path="logo.png", kind="modified", diff=None),),
            )
        ).plain

        self.assertIn("logo.png", rendered)
        self.assertIn("not shown as text", rendered)

    def test_truncation_is_announced(self):
        rendered = render_changes(
            WorkspaceChanges(
                label="turn",
                changes=(FileChange(path="a.py", kind="modified", diff=None),),
                total=9,
            )
        ).plain

        self.assertIn("More files changed", rendered)


class ChangesViewerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _initialise(self) -> None:
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"def parse(raw):\n    return raw\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def _app(self, service: CheckpointService | None, client=None) -> TrueCoderApp:
        agent = Agent(
            llm_client=client or ScriptedLLMClient([_reply()]),
            project_root=self.root,
            checkpoints=service,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def test_a_shell_mediated_change_is_shown(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        await service.capture("before the turn")
        _run(self.root, "sed", "-i", "s/return raw/return int(raw)/", "app.py")
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_review_changes()
            content = await wait_for(pilot, app, "#changes-content")

            rendered = str(content.render())
            self.assertIn("app.py", rendered)
            self.assertIn("return int(raw)", rendered)

    async def test_an_untouched_workspace_reports_nothing(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        await service.capture("before the turn")
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_review_changes()
            content = await wait_for(pilot, app, "#changes-content")

            self.assertIn("Nothing changed", str(content.render()))

    async def test_a_non_repository_explains_itself(self):
        service = CheckpointService(GitWorkspace(self.root))
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_review_changes()
            reason = await wait_for(pilot, app, "#changes-unavailable")

            self.assertIn("not a git repository", str(reason.render()))

    async def test_no_checkpoint_yet_is_explained(self):
        self._initialise()
        app = self._app(CheckpointService(GitWorkspace(self.root)))

        async with running(app) as pilot:
            await app.action_review_changes()

            self.assertTrue(await wait_for(pilot, app, "#changes-empty"))

    async def test_the_summary_counts_the_changed_files(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        await service.capture("before the turn")
        (self.root / "app.py").write_bytes(b"changed\n")
        app = self._app(service)

        async with running(app) as pilot:
            await app.action_review_changes()
            summary = await wait_for(pilot, app, "#changes-summary")

            self.assertIn("1 file(s) changed", str(summary.render()))

    async def test_no_service_is_reported_rather_than_crashing(self):
        app = self._app(None)

        async with running(app):
            await app.action_review_changes()

            self.assertNotIsInstance(app.screen, WorkspaceChangesScreen)

    async def test_a_finished_turn_announces_what_changed(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        client = MutatingClient(
            [_reply()],
            self.root / "app.py",
            b"agent changed this\n",
        )
        app = self._app(service, client)
        notices: list[str] = []
        app.notify = lambda message, **kwargs: notices.append(str(message))

        async with running(app) as pilot:
            app.query_one("#prompt-input").text = "change it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: not app._busy,
                description="the turn to finish",
            )

        self.assertTrue(any("file(s) changed" in notice for notice in notices))

    async def test_a_turn_that_changed_nothing_stays_quiet(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        app = self._app(service)
        notices: list[str] = []
        app.notify = lambda message, **kwargs: notices.append(str(message))

        async with running(app) as pilot:
            app.query_one("#prompt-input").text = "just answer"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: not app._busy,
                description="the turn to finish",
            )

        self.assertFalse(any("file(s) changed" in notice for notice in notices))


if __name__ == "__main__":
    unittest.main()
