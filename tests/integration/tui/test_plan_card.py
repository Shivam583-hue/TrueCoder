"""Integration coverage for the plan card and its place in the transcript."""

import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import (
    FixedTokenCounter,
    ScriptedLLMClient,
    tool_cards,
)
from truecoder.agent import Agent, ContextBuilder
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.planning import PlanStore
from truecoder.tools import ToolCall
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.widgets import ChatMessage, PlanCard, PromptInput, StatusBar


def plan_arguments(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {"steps": [{"title": title, "status": status} for title, status in pairs]}
    )


def plan_call(call_id: str, arguments: str) -> list[StreamEvent]:
    return [
        StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            tool_calls=(ToolCall(call_id, "update_plan", arguments),),
            finish_reason="tool_calls",
        )
    ]


def final_text(text: str = "All done") -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
    ]


def make_planning_agent(client: ScriptedLLMClient, store: PlanStore) -> Agent:
    return Agent(
        llm_client=client,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=1000,
            token_counter=FixedTokenCounter(),
        ),
        plan_store=store,
    )


def plan_cards(app: TrueCoderApp) -> list[PlanCard]:
    return list(app.query(PlanCard))


@asynccontextmanager
async def completed_turn(app: TrueCoderApp, prompt: str = "do the work"):
    with patch.dict(os.environ, {"MODEL": "test-model"}):
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = prompt
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: not app._busy,
                description="the turn to finish",
            )
            yield pilot


class PlanCardTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_plan_call_mounts_one_card_and_no_tool_card(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call(
                    "call_1",
                    plan_arguments(
                        ("Read the failing test", "done"),
                        ("Fix the parser", "in_progress"),
                        ("Run the suite", "pending"),
                    ),
                ),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            cards = plan_cards(app)

            self.assertEqual(len(cards), 1)
            self.assertEqual(tool_cards(app), [])
            self.assertEqual(cards[0].plan.total, 3)
            self.assertEqual(cards[0].plan.completed, 1)

    async def test_a_later_call_updates_the_same_card(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call(
                    "call_1",
                    plan_arguments(
                        ("Fix the parser", "in_progress"),
                        ("Run the suite", "pending"),
                    ),
                ),
                plan_call(
                    "call_2",
                    plan_arguments(
                        ("Fix the parser", "done"),
                        ("Run the suite", "in_progress"),
                    ),
                ),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            cards = plan_cards(app)

            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].plan.completed, 1)
            assert cards[0].plan.active_step is not None
            self.assertEqual(cards[0].plan.active_step.title, "Run the suite")

    async def test_the_status_bar_tracks_plan_progress(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call(
                    "call_1",
                    plan_arguments(
                        ("Fix the parser", "done"),
                        ("Run the suite", "in_progress"),
                    ),
                ),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            status_bar = app.query_one(StatusBar)
            label = status_bar.query_one("#footer-status").render()

            self.assertIn("plan 1/2", str(label))

    async def test_a_completed_plan_is_marked_complete(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call(
                    "call_1",
                    plan_arguments(("Fix the parser", "done")),
                ),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            card = plan_cards(app)[0]

            self.assertTrue(card.plan.is_complete)
            self.assertIn("complete", card.classes)

    async def test_the_plan_card_sits_above_the_following_reply(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call("call_1", plan_arguments(("Fix the parser", "pending"))),
                final_text("Finished the work"),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            transcript = list(app.query("#transcript > *"))
            plan_index = next(
                index
                for index, widget in enumerate(transcript)
                if isinstance(widget, PlanCard)
            )
            reply_index = next(
                index
                for index, widget in enumerate(transcript)
                if isinstance(widget, ChatMessage)
                and "Finished the work" in widget.content_text
            )

            self.assertLess(plan_index, reply_index)

    async def test_an_invalid_plan_shows_a_failed_tool_card(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call(
                    "call_1",
                    plan_arguments(("A", "in_progress"), ("B", "in_progress")),
                ),
                final_text("Sorry"),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            cards = tool_cards(app)

            self.assertEqual(plan_cards(app), [])
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].tool_name, "update_plan")
            self.assertEqual(cards[0].state, "failed")

    async def test_a_new_chat_clears_the_plan(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call("call_1", plan_arguments(("Fix the parser", "pending"))),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            self.assertEqual(len(plan_cards(app)), 1)

            await app.action_new_chat()

            self.assertEqual(plan_cards(app), [])
            self.assertIsNone(store.current)
            self.assertIsNone(app._plan_card)
            self.assertEqual(app._plan_calls, {})

    async def test_the_plan_reaches_the_following_request(self):
        store = PlanStore()
        client = ScriptedLLMClient(
            [
                plan_call("call_1", plan_arguments(("Fix the parser", "in_progress"))),
                final_text(),
            ]
        )
        app = TrueCoderApp(make_planning_agent(client, store))

        async with completed_turn(app):
            second_request = client.calls[1][0]

            assert store.current is not None
            self.assertEqual(second_request[-1]["role"], "system")
            self.assertEqual(second_request[-1]["content"], store.current.render())


if __name__ == "__main__":
    unittest.main()
