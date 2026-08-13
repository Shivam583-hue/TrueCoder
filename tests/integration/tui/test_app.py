"""Integration coverage for the Textual app, agent, and tool timeline."""

import asyncio
import os
import unittest
from unittest.mock import Mock, patch

from tests.helpers.tui import click_when_ready, wait_until
from truecoder.agent import Agent, AgentMode, ContextBuilder
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
)
from truecoder.providers import SessionSettings, settings_from_environment
from truecoder.tools import ToolApproval, ToolArguments, ToolCall, ToolRegistry
from truecoder.tools.base import BaseTool
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.modes import FullAccessScreen
from truecoder.tui.widgets import (
    ChatMessage,
    Composer,
    EmptyState,
    PromptInput,
    StatusBar,
    ToolCallCard,
)


def environment_settings() -> SessionSettings:
    return settings_from_environment()


class FakeLLMClient:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.events = events
        self.calls: list[tuple[list[dict], bool]] = []
        self.closed = False
        self._settings: SessionSettings | None = None

    @property
    def settings(self) -> SessionSettings:
        if self._settings is None:
            self._settings = environment_settings()
        return self._settings

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

    async def run(self, arguments: GuardedArguments, invocation=None) -> dict[str, str]:
        del invocation
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
                StreamEvent(
                    type=EventType.TEXT_DELTA, text_delta=TextDelta("All done")
                ),
                StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
            ],
        ]
    )


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


def make_agent(
    client: FakeLLMClient, tool_registry: ToolRegistry | None = None
) -> Agent:
    return Agent(
        llm_client=client,
        tool_registry=tool_registry,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100,
            token_counter=FixedTokenCounter(),
        ),
    )


def tool_cards(app: TrueCoderApp) -> list[ToolCallCard]:
    return list(app.query(ToolCallCard))


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
                self.assertTrue(app.query_one(EmptyState).display)
                self.assertTrue(app.screen.has_class("empty-chat"))
                logo_lines = str(app.query_one("#ascii-logo").content).splitlines()
                self.assertEqual(len(logo_lines), 3)
                self.assertEqual({len(line) for line in logo_lines}, {27})
                self.assertEqual(len(app.query("#splash-tagline")), 0)
                self.assertEqual(len(app.query("#topbar")), 0)
                self.assertTrue(app.query_one(StatusBar).display)
                self.assertEqual(len(app.query("#app-status")), 0)
                metadata = str(app.query_one("#composer-metadata").content)
                self.assertIn("Build", metadata)
                self.assertIn("test-model", metadata)
                self.assertNotIn("xhigh", metadata)
                self.assertNotIn("Enter to start", metadata)
                self.assertTrue(app.query_one("#launcher-shortcuts").display)
                shortcuts = str(app.query_one("#launcher-shortcuts").content)
                self.assertIn(
                    "/ commands",
                    shortcuts,
                )
                self.assertIn("shift+tab mode", shortcuts)
                self.assertIn(
                    "ctrl+p sessions",
                    shortcuts,
                )
                self.assertIn("ctrl+q quit", shortcuts)
                self.assertTrue(app.query_one("#launcher-tip").display)
                self.assertIn("Tip", str(app.query_one("#launcher-tip").content))
                composer_shell = app.query_one("#composer-shell")
                prompt_input = app.query_one(PromptInput)
                self.assertEqual(composer_shell.region.height, 5)
                self.assertEqual(
                    prompt_input.region.y,
                    composer_shell.region.y + 1,
                )
                self.assertEqual(
                    app.query_one("#transcript").region.x,
                    composer_shell.region.x,
                )

        self.assertTrue(client.closed)

    async def test_shift_tab_cycles_modes_with_full_access_confirmation(self):
        app = TrueCoderApp(make_agent(FakeLLMClient([])))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.PLAN)
            self.assertIn(
                "Plan",
                str(app.query_one("#composer-metadata").content),
            )

            await pilot.press("shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.PLAN)
            self.assertIsInstance(app.screen, FullAccessScreen)
            copy = " ".join(
                str(widget.content)
                for widget in app.screen.query(".session-delete-copy")
            )
            self.assertIn("stop asking", copy)
            self.assertIn("Hard policy denials", copy)
            await click_when_ready(
                pilot,
                app.screen.query_one("#full-access-confirm"),
            )
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.FULL_ACCESS)
            self.assertIn(
                "Full Access",
                str(app.query_one("#composer-metadata").content),
            )

            await pilot.press("shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.BUILD)

            await pilot.press("shift+tab", "shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.FULL_ACCESS)
            self.assertNotIsInstance(app.screen, FullAccessScreen)

    async def test_escape_cancels_full_access_without_changing_mode(self):
        app = TrueCoderApp(make_agent(FakeLLMClient([])))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("shift+tab", "shift+tab")
            await pilot.pause()
            self.assertIsInstance(app.screen, FullAccessScreen)

            await pilot.press("escape")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.PLAN)
            self.assertNotIsInstance(app.screen, FullAccessScreen)
            self.assertIn(
                "Plan",
                str(app.query_one("#composer-metadata").content),
            )

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
            self.assertFalse(app.screen.has_class("empty-chat"))
            self.assertEqual(len(app.query("#topbar")), 0)
            self.assertTrue(app.query_one(StatusBar).display)
            self.assertGreater(
                app.query_one("#composer-shell").region.y,
                app.screen.region.height * 2 // 3,
            )
            transcript_width = app.query_one("#transcript").content_region.width
            user_message, assistant_message = messages
            self.assertEqual(user_message.region.width, assistant_message.region.width)
            self.assertLessEqual(assistant_message.region.width, transcript_width)
            self.assertEqual(assistant_message.region.width, transcript_width)
            self.assertEqual(user_message.styles.border_left[0], "solid")
            self.assertEqual(assistant_message.styles.border_left[0], "")
            self.assertEqual(assistant_message.styles.background.a, 0)
            self.assertEqual(len(user_message.query(".message-header")), 0)
            self.assertEqual(user_message.styles.padding.top, 1)
            self.assertEqual(user_message.styles.padding.bottom, 1)
            self.assertGreaterEqual(user_message.region.height, 4)
            footer = str(assistant_message.query_one(".message-footer").content)
            self.assertIn("Build", footer)
            self.assertIn(app._model_name, footer)
            self.assertNotIn("xhigh", footer)
            self.assertFalse(app.query_one("#launcher-shortcuts").display)
            self.assertFalse(app.query_one("#launcher-tip").display)
            footer_status = str(app.query_one("#footer-status").content)
            self.assertIn("ctrl+p sessions", footer_status)
            self.assertIn("ctrl+a audit", footer_status)
            self.assertLessEqual(
                len(footer_status),
                app.query_one("#footer-status").region.width,
            )

    async def test_successful_turn_is_saved_to_the_active_session(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Answer"),
                ),
                StreamEvent(type=EventType.MESSAGE_COMPLETE),
            ]
        )
        manager = Mock()
        app = TrueCoderApp(make_agent(client), session_manager=manager)

        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "Question"
            await pilot.press("enter")
            await app.workers.wait_for_complete()

        manager.save_completed_turns.assert_called_once_with()
        manager.close.assert_called_once_with()

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
            self.assertGreaterEqual(prompt.region.height, 3)
            self.assertEqual(client.calls, [])

    async def test_composer_grows_for_soft_wrapped_content(self):
        app = TrueCoderApp(make_agent(FakeLLMClient([])))

        async with app.run_test(size=(48, 24)) as pilot:
            prompt = app.query_one(PromptInput)
            prompt.text = "inspect " * 18
            await pilot.pause()

            self.assertGreater(prompt.region.height, 3)
            self.assertLessEqual(prompt.region.height, 8)

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

    async def test_new_chat_creates_a_persisted_session_when_configured(self):
        manager = Mock()
        app = TrueCoderApp(
            make_agent(FakeLLMClient([])),
            session_manager=manager,
        )

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause()

        manager.create_session.assert_called_once_with()

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
            self.assertTrue(app.query_one(Composer).has_class("busy"))

            await pilot.press("ctrl+l")
            await pilot.pause()

            self.assertFalse(app._busy)
            self.assertEqual(list(app.query(ChatMessage)), [])
            self.assertEqual(app.messages, [])
            self.assertTrue(app.query_one(EmptyState).display)
            self.assertFalse(app.query_one(Composer).has_class("busy"))

    async def test_mode_change_during_response_applies_to_the_next_turn(self):
        client = BlockingLLMClient([])
        app = TrueCoderApp(make_agent(client))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "Long request"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: app.agent.active_mode is AgentMode.BUILD,
                description="the build-mode turn to start",
            )

            await pilot.press("shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.PLAN)
            self.assertIs(app.agent.active_mode, AgentMode.BUILD)
            self.assertIn(
                "Plan",
                str(app.query_one("#composer-metadata").content),
            )
            assistant = list(app.query(ChatMessage))[-1]
            self.assertIs(assistant.mode, AgentMode.BUILD)
            self.assertIn(
                "Build",
                str(assistant.query_one(".message-footer").content),
            )

            await pilot.press("escape")
            await app.workers.wait_for_complete()


class TrueCoderAppApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_approval_must_be_resolved_before_switching_modes(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: app._pending_approval is not None,
                description="the approval request",
            )

            await pilot.press("shift+tab")
            await pilot.pause()

            self.assertIs(app.agent.mode, AgentMode.BUILD)
            self.assertIsNotNone(app._pending_approval)
            self.assertIn(
                "Build",
                str(app.query_one("#composer-metadata").content),
            )

            await click_when_ready(pilot, app.query_one(".approval-reject"))
            await app.workers.wait_for_complete()

    async def test_text_tool_and_final_response_keep_stream_order(self):
        tool = GuardedTool()
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("I'll inspect it."),
                    ),
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(
                            ToolCall("call_1", "guarded", '{"text": "hi"}'),
                        ),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("The file is ready."),
                    ),
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        finish_reason="stop",
                    ),
                ],
            ]
        )
        app = TrueCoderApp(make_agent(client, registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "inspect it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: bool(tool_cards(app))
                and tool_cards(app)[-1].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the tool call to await approval",
            )

            transcript = app.query_one("#transcript")
            timeline = [
                widget
                for widget in transcript.children
                if isinstance(widget, (ChatMessage, ToolCallCard))
            ]
            self.assertEqual(
                [type(widget) for widget in timeline],
                [ChatMessage, ChatMessage, ToolCallCard, ChatMessage],
            )
            preamble = timeline[1]
            self.assertIsInstance(preamble, ChatMessage)
            self.assertEqual(preamble.content_text, "I'll inspect it.")
            self.assertFalse(preamble.query_one(".message-footer").display)

            await click_when_ready(pilot, app.query_one(".approval-once"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            final_response = timeline[-1]
            self.assertIsInstance(final_response, ChatMessage)
            self.assertEqual(final_response.content_text, "The file is ready.")

    async def test_required_tool_waits_then_runs_when_approved(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: bool(tool_cards(app))
                and tool_cards(app)[-1].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the tool call to await approval",
            )
            await wait_until(
                pilot,
                lambda: tool_cards(app)[-1].region.width <= 104,
                description="the approval card to be laid out narrower",
            )

            cards = list(app.query(ToolCallCard))
            self.assertEqual(len(cards), 1)
            card = cards[0]
            self.assertEqual(card.tool_name, "guarded")
            self.assertEqual(card.state, "awaiting-approval")
            self.assertTrue(card.has_class("state-awaiting-approval"))
            self.assertTrue(card.query_one(".tool-approval-actions").display)
            self.assertLessEqual(card.region.width, 104)
            self.assertEqual(card.styles.padding.left, 2)
            self.assertEqual(card.styles.padding.right, 2)
            self.assertEqual(
                str(card.query_one(".tool-state-label").content),
                "Awaiting approval",
            )
            self.assertIn(
                "text=hi",
                str(card.query_one(".tool-parameters").content),
            )
            self.assertTrue(
                app.focused.has_class("approval-once")
                if app.focused is not None
                else False
            )
            self.assertEqual(tool.runs, 0)

            await click_when_ready(pilot, app.query_one(".approval-once"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 1)
            self.assertEqual(len(list(app.query(ToolCallCard))), 1)
            self.assertEqual(card.state, "completed")
            self.assertFalse(card.query_one(".tool-approval-actions").display)
            self.assertTrue(card.query_one(".tool-details-toggle").display)
            await click_when_ready(pilot, card.query_one(".tool-details-toggle"))
            await pilot.pause()
            self.assertTrue(card.has_class("expanded"))
            self.assertIn(
                '"echoed": "hi"',
                str(card.query_one(".tool-details-content").content),
            )
            self.assertIsNone(app._pending_approval)
            assistant = list(app.query(ChatMessage))[-1]
            self.assertIn("All done", assistant.content_text)

    async def test_required_tool_is_reported_back_when_rejected(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: bool(tool_cards(app))
                and tool_cards(app)[-1].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the tool call to await approval",
            )

            await click_when_ready(pilot, app.query_one(".approval-reject"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 0)
            cards = list(app.query(ToolCallCard))
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].state, "rejected")
            self.assertIn(
                "Rejected guarded",
                str(cards[0].query_one(".tool-title").content),
            )
            assistant = list(app.query(ChatMessage))[-1]
            self.assertIn("All done", assistant.content_text)

    async def test_exact_session_scope_reuses_without_prompting(self):
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
                    tool_calls=(ToolCall("call_2", "guarded", '{"text": "a"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA, text_delta=TextDelta("Done")
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ],
            ]
        )
        app = TrueCoderApp(make_agent(client, registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: bool(tool_cards(app))
                and tool_cards(app)[-1].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the tool call to await approval",
            )

            self.assertEqual(len(list(app.query(ToolCallCard))), 1)

            first_card = app.query_one(ToolCallCard)
            await click_when_ready(
                pilot,
                first_card.query_one(".approval-session"),
            )
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(tool.runs, 2)
            cards = list(app.query(ToolCallCard))
            self.assertEqual(len(cards), 2)
            self.assertTrue(all(card.state == "completed" for card in cards))

    async def test_changed_arguments_do_not_reuse_session_scope(self):
        tool = GuardedTool()
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "guarded", '{"text": "a"}'),),
                    )
                ],
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_2", "guarded", '{"text": "b"}'),),
                    )
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Done"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE),
                ],
            ]
        )
        app = TrueCoderApp(make_agent(client, registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: bool(tool_cards(app))
                and tool_cards(app)[0].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the first tool call to await approval",
            )

            first_card = app.query_one(ToolCallCard)
            await click_when_ready(
                pilot,
                first_card.query_one(".approval-session"),
            )
            await wait_until(
                pilot,
                lambda: len(tool_cards(app)) == 2
                and tool_cards(app)[1].state == "awaiting-approval"
                and app._pending_approval is not None,
                description="the changed arguments to ask for approval again",
            )

            cards = tool_cards(app)
            self.assertEqual(tool.runs, 1)
            self.assertEqual(len(cards), 2)
            self.assertEqual(cards[1].state, "awaiting-approval")

            await click_when_ready(
                pilot,
                cards[1].query_one(".approval-reject"),
            )
            await app.workers.wait_for_complete()

    async def test_new_chat_cancels_a_pending_approval(self):
        tool = GuardedTool()
        app = TrueCoderApp(make_agent(guarded_tool_call(), registry_with(tool)))

        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(PromptInput).text = "read it"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(len(list(app.query(ToolCallCard))), 1)

            await pilot.press("ctrl+l")
            await pilot.pause()

            self.assertEqual(tool.runs, 0)
            self.assertEqual(list(app.query(ToolCallCard)), [])
            self.assertEqual(app.messages, [])
            self.assertIsNone(app._pending_approval)
            self.assertFalse(app._busy)

    async def test_tool_card_variants_and_narrow_approval_layout(self):
        app = TrueCoderApp(make_agent(FakeLLMClient([])))

        async with app.run_test(size=(48, 30)) as pilot:
            app.screen.remove_class("empty-chat")
            app.query_one("#empty-state").styles.display = "none"
            transcript = app.query_one("#transcript")
            card = ToolCallCard(
                "call_shell",
                "run_shell",
                {"command": "git status --short"},
                state="queued",
            )
            await transcript.mount(card)
            await pilot.pause()

            self.assertTrue(card.has_class("risky"))
            self.assertEqual(card.state, "queued")
            card.set_state("running")
            self.assertEqual(card.state, "running")
            card.finish(
                "error",
                '{"error": "command failed", "status": "error"}',
            )
            self.assertEqual(card.state, "failed")
            self.assertTrue(card.has_class("state-failed"))

            approval = ToolCallCard(
                "call_write",
                "write_file",
                {"path": "src/example.py", "content": "pass"},
                state="awaiting-approval",
                allowed_approval_scopes=("once", "session", "workspace"),
            )
            await transcript.mount(approval)
            await pilot.pause()

            once = approval.query_one(".approval-once")
            session = approval.query_one(".approval-session")
            workspace = approval.query_one(".approval-workspace")
            reject = approval.query_one(".approval-reject")
            self.assertEqual(once.region.y, session.region.y)
            self.assertEqual(workspace.region.y, reject.region.y)
            self.assertLess(once.region.y, workspace.region.y)
            self.assertEqual(
                str(approval.query_one(".tool-target").content),
                "src/example.py",
            )
            self.assertTrue(approval.has_class("risky"))
            self.assertIn(
                '"content": "pass"',
                str(approval.query_one(".tool-details-content").content),
            )

            shell_approval = ToolCallCard(
                "call_shell_approval",
                "shell",
                {"command": "pytest -q && ruff check ."},
                state="awaiting-approval",
            )
            await transcript.mount(shell_approval)
            await pilot.pause()
            self.assertTrue(shell_approval.has_class("approval-once-only"))
            self.assertTrue(shell_approval.query_one(".approval-once").display)
            self.assertFalse(
                shell_approval.query_one(".approval-session").display
            )
            self.assertFalse(
                shell_approval.query_one(".approval-workspace").display
            )
            self.assertTrue(shell_approval.query_one(".approval-reject").display)
            approval.finish(
                "success",
                (
                    '{"status":"success","output":{"path":"src/example.py",'
                    '"created":true,"bytes_written":4}}'
                ),
            )
            self.assertIn(
                "Wrote src/example.py · 4 bytes",
                str(approval.query_one(".tool-title").content),
            )

            rejected_write = ToolCallCard(
                "call_rejected_write",
                "write_file",
                {"path": "src/rejected.py", "content": "pass"},
                state="running",
            )
            await transcript.mount(rejected_write)
            await pilot.pause()
            rejected_write.reject(
                '{"status":"error","error":"The user rejected this tool call.",'
                '"error_code":"approval_rejected"}'
            )
            self.assertIn(
                "Rejected write file src/rejected.py",
                str(rejected_write.query_one(".tool-title").content),
            )

            read_card = ToolCallCard(
                "call_read",
                "read_file",
                {
                    "path": "pyproject.toml",
                    "start_line": 1,
                    "line_count": 100,
                },
                state="running",
            )
            await transcript.mount(read_card)
            await pilot.pause()
            read_card.finish(
                "success",
                (
                    '{"status":"success","output":{"path":"pyproject.toml",'
                    '"start_line":1,"end_line":42}}'
                ),
            )
            self.assertIn(
                "Read pyproject.toml · 42 lines",
                str(read_card.query_one(".tool-title").content),
            )

            repository_cards = (
                (
                    "edit_file",
                    {
                        "path": "src/example.py",
                        "edits": [
                            {"old_text": "pass", "new_text": "value = 1"}
                        ],
                    },
                    (
                        '{"status":"success","output":{"path":"src/example.py",'
                        '"replacements":1,"bytes_written":9}}'
                    ),
                    "Edited src/example.py · 1 replacement",
                    True,
                ),
                (
                    "list_dir",
                    {"path": "src"},
                    (
                        '{"status":"success","output":{"path":"src","entries":'
                        '[{"path":"src/truecoder","name":"truecoder",'
                        '"type":"directory"}],"has_more":false}}'
                    ),
                    "Listed src · 1 entry",
                    False,
                ),
                (
                    "glob",
                    {"path": ".", "pattern": "**/*.py"},
                    (
                        '{"status":"success","output":{"path":".","pattern":'
                        '"**/*.py","matches":["src/app.py","tests/test_app.py"],'
                        '"has_more":false}}'
                    ),
                    "Matched . · 2 matches",
                    False,
                ),
                (
                    "grep",
                    {"path": ".", "pattern": "Agent"},
                    (
                        '{"status":"success","output":{"path":".","pattern":'
                        '"Agent","matches":[],"has_more":false}}'
                    ),
                    "Searched . · 0 matches",
                    False,
                ),
            )
            for index, (
                tool_name,
                arguments,
                result,
                expected_headline,
                risky,
            ) in enumerate(repository_cards):
                with self.subTest(tool_name=tool_name):
                    repository_card = ToolCallCard(
                        f"call_repository_{index}",
                        tool_name,
                        arguments,
                        state="running",
                    )
                    await transcript.mount(repository_card)
                    await pilot.pause()
                    repository_card.finish("success", result)
                    self.assertIn(
                        expected_headline,
                        str(repository_card.query_one(".tool-title").content),
                    )
                    self.assertEqual(repository_card.has_class("risky"), risky)


if __name__ == "__main__":
    unittest.main()
