"""A tool call the model gets wrong must not take down the turn."""

import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import (
    FixedTokenCounter,
    ScriptedLLMClient,
    tool_cards,
)
from truecoder.agent import Agent, ContextBuilder
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tools import ToolCall, ToolRegistry
from truecoder.tools.builtin import ReadFileTool
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.widgets import ChatMessage, PromptInput


def _turn(arguments: str):
    return ScriptedLLMClient(
        [
            [
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    tool_calls=(ToolCall("call_1", "read_file", arguments),),
                    finish_reason="tool_calls",
                )
            ],
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Sorry, I got that wrong."),
                ),
                StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
            ],
        ]
    )


class InvalidToolCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        (self.root / "README.md").write_bytes(b"# Project\n\nSome text.\n")
        self.addCleanup(self._directory.cleanup)

    def _app(self, arguments: str) -> TrueCoderApp:
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.root))
        agent = Agent(
            llm_client=_turn(arguments),
            tool_registry=registry,
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    @asynccontextmanager
    async def _run(self, app: TrueCoderApp, predicate, description: str):
        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "read the readme"
                await pilot.press("enter")
                await wait_until(pilot, predicate, description=description)
                yield pilot

    def _replies(self, app: TrueCoderApp) -> list[ChatMessage]:
        return [
            message
            for message in app.query(ChatMessage)
            if "I got that wrong" in message.content_text
        ]

    async def test_a_validation_error_does_not_kill_the_turn(self):
        app = self._app(json.dumps({"path": "README.md"}))

        async with self._run(
            app,
            lambda: bool(self._replies(app)),
            "the model to recover and reply",
        ):
            self.assertEqual(len(self._replies(app)), 1)

    async def test_the_failed_call_is_shown_as_a_failed_card(self):
        app = self._app(json.dumps({"path": "README.md", "start_line": 0}))

        async with self._run(
            app,
            lambda: any(card.state == "failed" for card in tool_cards(app)),
            "the failed tool card",
        ):
            cards = tool_cards(app)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].tool_name, "read_file")

    async def test_no_message_is_rendered_as_an_error(self):
        app = self._app(json.dumps({"path": "README.md"}))

        async with self._run(
            app,
            lambda: bool(self._replies(app)),
            "the model to recover and reply",
        ):
            failed = [m for m in app.query(ChatMessage) if m.has_class("error")]
            self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
