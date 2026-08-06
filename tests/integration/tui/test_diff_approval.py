"""Integration coverage for reviewing a file mutation before approving it."""

import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tools import ToolCall, ToolRegistry
from truecoder.tools.builtin import EditFileTool, WriteFileTool
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.widgets import PromptInput, ToolCallCard


def _turn(call: ToolCall) -> ScriptedLLMClient:
    return ScriptedLLMClient(
        [
            [
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                )
            ],
            [
                StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("Done")),
                StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
            ],
        ]
    )


@asynccontextmanager
async def awaiting_approval(app: TrueCoderApp):
    with patch.dict(os.environ, {"MODEL": "test-model"}):
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "change it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: any(
                    card.state == "awaiting-approval"
                    for card in app.query(ToolCallCard)
                )
                and app._pending_approval is not None,
                description="the mutation to await approval",
            )
            yield pilot


class DiffApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _app(self, call: ToolCall, registry: ToolRegistry) -> TrueCoderApp:
        agent = Agent(
            llm_client=_turn(call),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
            tool_registry=registry,
            project_root=self.workspace,
        )
        return TrueCoderApp(agent)

    def _edit_app(self) -> TrueCoderApp:
        registry = ToolRegistry()
        registry.register(EditFileTool(self.workspace))
        call = ToolCall(
            "call_1",
            "edit_file",
            json.dumps(
                {
                    "path": "a.py",
                    "old_text": "two",
                    "new_text": "TWO",
                    "replace_all": False,
                }
            ),
        )
        return self._app(call, registry)

    async def test_an_edit_approval_shows_the_rendered_diff(self):
        (self.workspace / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        app = self._edit_app()

        async with awaiting_approval(app):
            card = app.query_one(ToolCallCard)
            rendered = card.query_one(".tool-diff-content").render()

            self.assertIsNotNone(card.mutation)
            self.assertIn("- ", str(rendered))
            self.assertIn("+ ", str(rendered))
            self.assertIn("TWO", str(rendered))
            app.reject_pending_tool()

    async def test_the_diff_region_is_visible_while_awaiting_approval(self):
        (self.workspace / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        app = self._edit_app()

        async with awaiting_approval(app):
            card = app.query_one(ToolCallCard)

            self.assertIn("has-diff", card.classes)
            self.assertTrue(card.query_one(".tool-diff").display)
            app.reject_pending_tool()

    async def test_the_summary_shows_the_diff_stat_instead_of_raw_arguments(self):
        (self.workspace / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        app = self._edit_app()

        async with awaiting_approval(app):
            card = app.query_one(ToolCallCard)
            summary = str(card.query_one(".tool-parameters").render())

            self.assertIn("+1", summary)
            self.assertIn("-1", summary)
            self.assertNotIn("old text", summary)
            app.reject_pending_tool()

    async def test_a_new_file_shows_an_all_addition_diff(self):
        registry = ToolRegistry()
        registry.register(WriteFileTool(self.workspace))
        call = ToolCall(
            "call_1",
            "write_file",
            json.dumps({"path": "new.py", "content": "alpha\nbeta\n"}),
        )
        app = self._app(call, registry)

        async with awaiting_approval(app):
            card = app.query_one(ToolCallCard)

            assert card.mutation is not None
            self.assertEqual(card.mutation.kind, "create")
            self.assertEqual((card.mutation.added, card.mutation.removed), (2, 0))
            app.reject_pending_tool()

    async def test_the_diff_disappears_once_the_call_completes(self):
        (self.workspace / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        app = self._edit_app()

        async with awaiting_approval(app) as pilot:
            card = app.query_one(ToolCallCard)
            app.approve_pending_tool_once()
            await wait_until(
                pilot,
                lambda: card.state == "completed",
                description="the edit to complete",
            )

            self.assertFalse(card.query_one(".tool-diff").display)
            self.assertEqual(
                (self.workspace / "a.py").read_text(encoding="utf-8"),
                "one\nTWO\nthree\n",
            )


if __name__ == "__main__":
    unittest.main()
