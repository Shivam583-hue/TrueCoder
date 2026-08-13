"""Reasoning effort changes are immediate, visible, and durable."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.providers.models import ApiKey, SessionSettings
from truecoder.providers.openai import openai_provider
from truecoder.providers.store import load_selection
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.effort_picker import ReasoningEffortItem, ReasoningEffortScreen
from truecoder.tui.widgets import Composer


class _EffortClient(ScriptedLLMClient):
    def __init__(self) -> None:
        super().__init__([])
        self._settings = SessionSettings(
            provider=openai_provider(),
            credential=ApiKey("sk-test"),
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        )


def _app() -> TrueCoderApp:
    agent = Agent(
        llm_client=_EffortClient(),
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=1000,
            token_counter=FixedTokenCounter(),
        ),
    )
    return TrueCoderApp(agent)


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.settings_path = Path(self._directory.name) / "settings.json"
        path = patch(
            "truecoder.providers.store.default_settings_path",
            return_value=self.settings_path,
        )
        path.start()
        self.addCleanup(path.stop)

    async def test_slash_effort_opens_only_the_models_supported_choices(self):
        app = _app()

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press(*"/effort")
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, ReasoningEffortScreen),
                description="the reasoning effort picker to open",
            )

            choices = [item.effort for item in app.screen.query(ReasoningEffortItem)]
            self.assertEqual(
                choices,
                ["none", "low", "medium", "high", "xhigh", "max"],
            )

            await pilot.press("up", "enter")
            await wait_until(
                pilot,
                lambda: not isinstance(app.screen, ReasoningEffortScreen),
                description="the reasoning effort picker to close",
            )

            settings = app.agent.llm_client.settings
            self.assertEqual(settings.reasoning_effort, "high")
            metadata = str(
                app.query_one(Composer).query_one("#composer-metadata").content
            )
            self.assertIn("high", metadata)
            self.assertNotIn("xhigh", metadata)

        self.assertEqual(load_selection(self.settings_path).reasoning_effort, "high")

    async def test_an_argument_changes_effort_without_opening_the_picker(self):
        app = _app()

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press(*"/effort low")
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: app.agent.llm_client.settings.reasoning_effort == "low",
                description="the reasoning effort to change",
            )

            self.assertNotIsInstance(app.screen, ReasoningEffortScreen)
            self.assertIn(
                "low",
                str(app.query_one("#composer-metadata").content),
            )


if __name__ == "__main__":
    unittest.main()
