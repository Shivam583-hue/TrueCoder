from __future__ import annotations

import asyncio
import os
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TextArea
from textual.worker import Worker, WorkerCancelled

from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage
from truecoder.tui.widgets import (
    ChatMessage,
    EmptyState,
    PromptInput,
    StatusBar,
    TopBar,
)


class TrueCoderApp(App[None]):
    """A polished terminal chat interface for TrueCoder."""

    CSS_PATH = "styles.tcss"
    TITLE = "TrueCoder"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (108, "-wide")]

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+l", "new_chat", "New chat", show=False, priority=True),
        Binding("escape", "cancel_response", "Stop", show=False, priority=True),
    ]

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        super().__init__()
        self.llm_client = llm_client or LLMClient()
        self.messages: list[dict[str, Any]] = []
        self._busy = False
        self._active_worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        model_name = os.getenv("MODEL") or "model not configured"
        yield TopBar(model_name)

        with Vertical(id="main"):
            with VerticalScroll(id="transcript"):
                yield EmptyState(id="empty-state")

            with Vertical(id="composer-shell"):
                yield Static("MESSAGE", id="composer-label", markup=False)
                with Horizontal(id="composer-row"):
                    yield PromptInput(
                        id="prompt-input",
                        placeholder="Ask TrueCoder anything…",
                        soft_wrap=True,
                        show_line_numbers=False,
                        tab_behavior="focus",
                        compact=True,
                    )
                    yield Button("Send  ↵", id="send-button", disabled=True)
                yield Static(
                    "Enter to send  ·  Shift+Enter for a new line",
                    id="composer-help",
                    markup=False,
                )

        yield StatusBar()

    def on_mount(self) -> None:
        self.screen.add_class("empty-chat")
        self.query_one(PromptInput).focus()

    async def on_unmount(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        await self.llm_client.close()

    @on(PromptInput.Submitted)
    async def submit_from_keyboard(self, event: PromptInput.Submitted) -> None:
        await self._submit_prompt(event.value)

    @on(Button.Pressed, "#send-button")
    async def submit_from_button(self) -> None:
        await self._submit_prompt(self.query_one(PromptInput).text)

    @on(TextArea.Changed, "#prompt-input")
    def update_send_button(self) -> None:
        self._sync_send_button()

    async def _submit_prompt(self, raw_prompt: str) -> None:
        prompt = raw_prompt.strip()
        if not prompt:
            return
        if self._busy:
            return

        self.screen.remove_class("empty-chat")
        prompt_input = self.query_one(PromptInput)
        prompt_input.text = ""

        self.query_one("#empty-state", EmptyState).styles.display = "none"
        transcript = self.query_one("#transcript", VerticalScroll)

        user_message = ChatMessage("user", prompt)
        assistant_message = ChatMessage("assistant")
        await transcript.mount(user_message, assistant_message)
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

        self.messages.append({"role": "user", "content": prompt})
        request_messages = [message.copy() for message in self.messages]

        self._set_busy(True)
        self._active_worker = self._stream_completion(
            request_messages,
            assistant_message,
        )

    @work(group="completion", exclusive=True, exit_on_error=False)
    async def _stream_completion(
        self,
        request_messages: list[dict[str, Any]],
        assistant_message: ChatMessage,
    ) -> None:
        response_text = ""
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        outcome = "ready"

        try:
            async for event in self.llm_client.chat_completion(
                request_messages,
                stream=True,
            ):
                if event.type == EventType.TEXT_DELTA and event.text_delta is not None:
                    response_text += event.text_delta.content
                    await assistant_message.append_delta(event.text_delta.content)
                    self._scroll_to_latest()
                elif event.type == EventType.MESSAGE_COMPLETE:
                    if event.text_delta is not None:
                        response_text += event.text_delta.content
                        await assistant_message.append_delta(event.text_delta.content)
                    usage = event.usage
                    finish_reason = event.finish_reason
                    completed = True
                elif event.type == EventType.ERROR:
                    await assistant_message.show_error(
                        event.error or "The request failed without an error message."
                    )
                    outcome = "error"
                    break

            if outcome != "error":
                if not completed:
                    await assistant_message.show_error(
                        "The response stream ended before completion."
                    )
                    outcome = "error"
                elif not response_text:
                    await assistant_message.show_error(
                        "The model completed without returning any text."
                    )
                    outcome = "error"
                else:
                    assistant_message.finish(usage, finish_reason)
                    self.messages.append(
                        {"role": "assistant", "content": response_text}
                    )
        except asyncio.CancelledError:
            await assistant_message.show_cancelled()
            outcome = "stopped"
        except Exception as error:
            await assistant_message.show_error(str(error))
            outcome = "error"
        finally:
            self._set_busy(False)
            self._scroll_to_latest()
            self.query_one(PromptInput).focus()

    def _scroll_to_latest(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#composer-shell").set_class(busy, "busy")
        self._sync_send_button()

    def _sync_send_button(self) -> None:
        prompt = self.query_one(PromptInput).text.strip()
        self.query_one("#send-button", Button).disabled = self._busy or not prompt

    async def action_new_chat(self) -> None:
        active_worker = self._active_worker
        if active_worker is not None and active_worker.is_running:
            active_worker.cancel()
            try:
                await active_worker.wait()
            except WorkerCancelled:
                pass
        self.messages.clear()
        await self.query(".chat-message").remove()
        self.query_one("#empty-state", EmptyState).styles.display = "block"
        self._set_busy(False)
        self.screen.add_class("empty-chat")
        self.query_one(PromptInput).focus()

    def action_cancel_response(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        else:
            self.query_one(PromptInput).focus()
