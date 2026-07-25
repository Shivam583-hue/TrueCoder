import asyncio
import os
import unittest
from unittest.mock import patch

from truecoder.agent import Agent, ContextBuilder
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
)
from truecoder.tools import ToolApproval, ToolArguments, ToolCall, ToolRegistry
from truecoder.tools.base import BaseTool
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.widgets import ApprovalCard, ChatMessage, EmptyState, PromptInput


class FakeLLMClient:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.events = events
        self.calls: list[tuple[list[dict], bool]] = []
        self.closed = False

    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class ScriptedLLMClient(FakeLLMClient):
    def __init__(self, batches: list[list[StreamEvent]]) -> None:
        super().__init__([])
        self.batches = batches

    async def chat_completion(self, messages, stream=True, tools=None):
        index = len(self.calls)
        self.calls.append((messages, stream))
        batch = self.batches[index] if index < len(self.batches) else []
        for event in batch:
            yield event


class GuardedArguments(ToolArguments):
    text: str


class GuardedTool(BaseTool[GuardedArguments]):
    name = "guarded"
    description = "Echo that requires approval before running."
    arguments_type = GuardedArguments
    approval = ToolApproval.REQUIRED

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, arguments: GuardedArguments) -> dict[str, str]:
        self.runs += 1
        return {"echoed": arguments.text}


def guarded_tool_call() -> ScriptedLLMClient:
    return ScriptedLLMClient(
        [
            [
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    tool_calls=(ToolCall("call_1", "guarded", '{"text": "hi"}'),),
                    finish_reason="tool_calls",
                ),
            ],
            [
                StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("All done")),
                StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
            ],
        ]
    )


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


def make_agent(client: FakeLLMClient, tool_registry: ToolRegistry | None = None) -> Agent:
    return Agent(
        llm_client=client,
        tool_registry=tool_registry,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100,
            token_counter=FixedTokenCounter(),
        ),
    )


def registry_with(tool: GuardedTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


class BlockingLLMClient(FakeLLMClient):
    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        yield StreamEvent(
            type=EventType.TEXT_DELTA,
            text_delta=TextDelta("Partial response"),
        )
        await asyncio.Event().wait()


class TrueCoderAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_mounts_with_prompt_focused(self):
        client = FakeLLMClient([])
        app = TrueCoderApp(make_agent(client))

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertEqual(app.focused.id, "prompt-input")
                self.assertEqual(app.query_one("#model-name").content, "test-model")
                self.assertTrue(app.query_one(EmptyState).display)
                self.assertTrue(app.screen.has_class("empty-chat"))
                logo_lines = str(app.query_one("#ascii-logo").content).splitlines()
                self.assertEqual(len(logo_lines), 7)
                self.assertEqual(max(map(len, logo_lines)), 55)
                self.assertFalse(app.query_one("#topbar").display)
                self.assertFalse(app.query_one("#statusbar").display)
                self.assertEqual(len(app.query("#app-status")), 0)
                self.assertEqual(len(app.query("#workspace-name")), 0)
                self.assertEqual(
                    app.query_one("#transcript").region.x,
                    app.query_one("#composer-shell").region.x,
                )

        self.assertTrue(client.closed)

    async def test_enter_submits_and_streams_response(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Hello "),
                ),
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("**world**!"),
                ),
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    finish_reason="stop",
                    usage=TokenUsage(completion_tokens=3),
                ),
            ]
        )
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "Say hello"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            messages = list(app.query(ChatMessage))
            self.assertEqual(
                [(message.role, message.content_text) for message in messages],
                [
                    ("user", "Say hello"),
                    ("assistant", "Hello **world**!"),
                ],
            )
            self.assertEqual(
                client.calls,
                [
                    (
                        [
                            {"role": "system", "content": "test system"},
                            {"role": "user", "content": "Say hello"},
                        ],
                        True,
                    )
                ],
            )
            self.assertEqual(
                app.messages,
                [
                    {"role": "user", "content": "Say hello"},
                    {"role": "assistant", "content": "Hello **world**!"},
                ],
            )
            self.assertEqual(prompt.text, "")
            self.assertTrue(app.query_one("#send-button").disabled)
            self.assertFalse(app.screen.has_class("empty-chat"))
            self.assertTrue(app.query_one("#topbar").display)
            self.assertTrue(app.query_one("#statusbar").display)
            self.assertGreater(
                app.query_one("#composer-shell").region.y,
                app.screen.region.height * 2 // 3,
            )
            transcript_width = app.query_one("#transcript").content_region.width
            user_message, assistant_message = messages
            self.assertEqual(user_message.region.width, transcript_width)
            self.assertEqual(assistant_message.region.width, transcript_width)
            self.assertEqual(user_message.styles.border_left[0], "solid")
            self.assertEqual(assistant_message.styles.border_left[0], "")
            self.assertEqual(assistant_message.styles.background.a, 0)
            self.assertEqual(len(user_message.query(".message-header")), 0)
            self.assertEqual(user_message.styles.padding.top, 0)
            self.assertEqual(user_message.styles.padding.bottom, 0)
            self.assertEqual(user_message.region.height, 1)

    async def test_shift_enter_inserts_a_newline(self):
        client = FakeLLMClient([])
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "first line"
            prompt.move_cursor((0, len(prompt.text)))

            await pilot.press("shift+enter")
            await pilot.press("s", "e", "c", "o", "n", "d")
            await pilot.pause()

            self.assertEqual(prompt.text, "first line\nsecond")
            self.assertEqual(client.calls, [])

    async def test_error_event_is_rendered_in_the_transcript(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.ERROR,
                    error="Connection error: offline",
                )
            ]
        )
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "Hello?"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assistant = list(app.query(ChatMessage))[-1]
            self.assertTrue(assistant.has_class("error"))
            self.assertIn("Connection error: offline", assistant.content_text)

    async def test_new_chat_clears_messages(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Answer"),
                ),
                StreamEvent(type=EventType.MESSAGE_COMPLETE),
            ]
        )
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "Question"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+l")
            await pilot.pause()

            self.assertEqual(list(app.query(ChatMessage)), [])
            self.assertEqual(app.messages, [])
            self.assertTrue(app.query_one(EmptyState).display)
            self.assertTrue(app.screen.has_class("empty-chat"))

    async def test_new_chat_safely_cancels_an_active_response(self):
        client = BlockingLLMClient([])
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "Long request"
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(app._busy)
            self.assertEqual(len(list(app.query(ChatMessage))), 2)

            await pilot.press("ctrl+l")
            await pilot.pause()

            self.assertFalse(app._busy)
            self.assertEqual(list(app.query(ChatMessage)), [])
            self.assertEqual(app.messages, [])
            self.assertTrue(app.query_one(EmptyState).display)


class TrueCoderAppApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_tool_waits_then_runs_when_approved(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await pilot.pause()

            cards = list(app.query(ApprovalCard))
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].tool_name, "guarded")
            self.assertEqual(tool.runs, 0)

            await pilot.click(".approval-approve")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 1)
            self.assertEqual(list(app.query(ApprovalCard)), [])
            self.assertIsNone(app._pending_approval)
            assistant = list(app.query(ChatMessage))[-1]
            self.assertIn("All done", assistant.content_text)

    async def test_required_tool_is_reported_back_when_rejected(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await pilot.pause()

            await pilot.click(".approval-reject")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 0)
            self.assertEqual(list(app.query(ApprovalCard)), [])
            assistant = list(app.query(ChatMessage))[-1]
            self.assertIn("All done", assistant.content_text)

    async def test_always_stops_prompting_for_that_tool(self):
        tool = GuardedTool()
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "guarded", '{"text": "a"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_2", "guarded", '{"text": "b"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("Done")),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ],
            ]
        )
        app = TrueCoderApp(make_agent(client, registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(len(list(app.query(ApprovalCard))), 1)

            await pilot.click(".approval-always")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 2)
            self.assertIn("guarded", app._always_approved)
            self.assertEqual(list(app.query(ApprovalCard)), [])

    async def test_new_chat_cancels_a_pending_approval(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(len(list(app.query(ApprovalCard))), 1)

            await pilot.press("ctrl+l")
            await pilot.pause()

            self.assertEqual(tool.runs, 0)
            self.assertEqual(list(app.query(ApprovalCard)), [])
            self.assertEqual(app.messages, [])
            self.assertIsNone(app._pending_approval)
            self.assertFalse(app._busy)


if __name__ == "__main__":
    unittest.main()
