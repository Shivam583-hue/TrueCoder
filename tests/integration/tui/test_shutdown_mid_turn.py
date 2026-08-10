"""Closing the application during a turn must not raise out of the worker."""

import asyncio
import os
import unittest
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, environment_settings
from truecoder.agent import Agent, ContextBuilder
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.widgets import PromptInput


class _SlowLLMClient:
    @property
    def settings(self):
        return environment_settings()

    async def chat_completion(self, messages, stream=True, tools=None):
        yield StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("thinking"))
        await asyncio.sleep(30)
        yield StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop")

    async def close(self) -> None:
        return None


class ShutdownMidTurnTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> TrueCoderApp:
        agent = Agent(
            llm_client=_SlowLLMClient(),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def test_unmounting_while_a_turn_runs_does_not_raise(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "hello"
                await pilot.press("enter")
                await wait_until(
                    pilot,
                    lambda: app._busy,
                    description="the turn to start",
                )

                self.assertTrue(app._busy)

    async def test_the_composer_may_be_gone_when_the_turn_settles(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await app.query("Composer").remove()

                app._set_busy(False)

        self.assertFalse(app._busy)


if __name__ == "__main__":
    unittest.main()
