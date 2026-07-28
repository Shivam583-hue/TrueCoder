from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button
from textual.worker import Worker, WorkerCancelled

from truecoder.agent.agent import Agent
from truecoder.agent.approval import ApprovalDecision, ApprovalRequest
from truecoder.agent.events import AgentEventType
from truecoder.agent.messages import ModelMessage
from truecoder.client.response import TokenUsage
from truecoder.session import SessionError, SessionManager
from truecoder.tui.widgets import (
    ChatMessage,
    Composer,
    EmptyState,
    PromptInput,
    StatusBar,
    ToolCallCard,
)


@dataclass
class _PendingApproval:
    call_id: str
    tool_name: str
    future: asyncio.Future[ApprovalDecision]
    card: ToolCallCard


def _package_version() -> str:
    try:
        return version("truecoder")
    except PackageNotFoundError:
        return "0.1.0"


def _git_branch(workspace: str) -> str | None:
    """Read the nearest repository branch without spawning a subprocess."""
    current = Path(workspace).resolve()
    for directory in (current, *current.parents):
        head_path = directory / ".git" / "HEAD"
        try:
            head = head_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        prefix = "ref: refs/heads/"
        return head[len(prefix) :] if head.startswith(prefix) else None
    return None


class TrueCoderApp(App[None]):
    """A polished terminal chat interface for TrueCoder."""

    CSS_PATH = "styles.tcss"
    TITLE = "TrueCoder"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [
        (0, "-narrow"),
        (52, "-compact"),
        (108, "-wide"),
    ]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+l", "new_chat", "New chat", show=False, priority=True),
        Binding("escape", "cancel_response", "Stop", show=False, priority=True),
    ]

    def __init__(
        self,
        agent: Agent | None = None,
        *,
        session_manager: SessionManager | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent or Agent()
        self.session_manager = session_manager
        self.agent.approval_handler = self._request_tool_approval
        self._busy = False
        self._active_worker: Worker[None] | None = None
        self._active_assistant: ChatMessage | None = None
        self._pending_approval: _PendingApproval | None = None
        self._always_approved: set[str] = set()
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._model_name = "model not configured"

    @property
    def messages(self) -> list[ModelMessage]:
        """Expose conversation history for UI state inspection."""
        return self.agent.messages

    def compose(self) -> ComposeResult:
        self._model_name = os.getenv("MODEL") or "model not configured"
        workspace = os.getcwd()

        with Vertical(id="main"):
            with VerticalScroll(id="transcript"):
                yield EmptyState(id="empty-state")

            yield Composer(self._model_name)

        yield StatusBar(
            workspace,
            branch=_git_branch(workspace),
            version=_package_version(),
            max_input_tokens=self.agent.context_builder.max_input_tokens,
        )

    def on_mount(self) -> None:
        self.screen.add_class("empty-chat")
        self.query_one(PromptInput).focus()

    async def on_unmount(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        try:
            await self.agent.close()
        finally:
            if self.session_manager is not None:
                self.session_manager.close()

    @on(PromptInput.Submitted)
    async def submit_from_keyboard(self, event: PromptInput.Submitted) -> None:
        await self._submit_prompt(event.value)

    @on(Button.Pressed, ".approval-approve")
    def approve_pending_tool(self) -> None:
        self._resolve_pending_approval(ApprovalDecision.APPROVED)

    @on(Button.Pressed, ".approval-always")
    def always_approve_pending_tool(self) -> None:
        self._resolve_pending_approval(ApprovalDecision.APPROVED, always=True)

    @on(Button.Pressed, ".approval-reject")
    def reject_pending_tool(self) -> None:
        self._resolve_pending_approval(ApprovalDecision.REJECTED)

    async def _submit_prompt(self, raw_prompt: str) -> None:
        prompt = raw_prompt.strip()
        if not prompt:
            return
        if self._busy:
            return

        self.screen.remove_class("empty-chat")
        self.query_one(StatusBar).set_conversation_active(True)
        prompt_input = self.query_one(PromptInput)
        prompt_input.text = ""

        self.query_one("#empty-state", EmptyState).styles.display = "none"
        transcript = self.query_one("#transcript", VerticalScroll)

        user_message = ChatMessage(
            "user",
            prompt,
            model_name=self._model_name,
        )
        assistant_message = ChatMessage(
            "assistant",
            model_name=self._model_name,
        )
        await transcript.mount(user_message, assistant_message)
        self._active_assistant = assistant_message
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

        self._set_busy(True)
        self._active_worker = self._stream_completion(
            prompt,
            assistant_message,
        )

    @work(group="completion", exclusive=True, exit_on_error=False)
    async def _stream_completion(
        self,
        prompt: str,
        assistant_message: ChatMessage,
    ) -> None:
        response_text = ""
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        outcome = "ready"

        try:
            async for event in self.agent.run(prompt):
                if event.type == AgentEventType.TEXT_DELTA:
                    content = str(event.data.get("content", ""))
                    response_text += content
                    await assistant_message.append_delta(content)
                    self._scroll_to_latest()
                elif event.type == AgentEventType.TOOL_CALL:
                    assistant_message = await self._show_tool_call(
                        call_id=str(event.data.get("call_id", "")),
                        tool_name=str(event.data.get("name", "tool")),
                        arguments=str(event.data.get("arguments", "{}")),
                        assistant_message=assistant_message,
                    )
                elif event.type == AgentEventType.APPROVAL_REQUESTED:
                    call_id = str(event.data.get("call_id", ""))
                    card = self._tool_cards.get(call_id)
                    arguments = event.data.get("arguments")
                    if card is not None and isinstance(arguments, dict):
                        card.set_awaiting_approval(arguments)
                        self._scroll_to_latest()
                elif event.type == AgentEventType.TOOL_RESULT:
                    self._finish_tool_call(
                        call_id=str(event.data.get("call_id", "")),
                        status=str(event.data.get("status", "error")),
                        content=str(event.data.get("content", "")),
                    )
                elif event.type == AgentEventType.TOOL_REJECTED:
                    self._reject_tool_call(
                        call_id=str(event.data.get("call_id", "")),
                        content=str(event.data.get("content", "")),
                    )
                elif event.type == AgentEventType.AGENT_END:
                    usage_data = event.data.get("usage")
                    usage = (
                        TokenUsage(**usage_data)
                        if isinstance(usage_data, dict)
                        else None
                    )
                    finish_reason_value = event.data.get("finish_reason")
                    finish_reason = (
                        str(finish_reason_value)
                        if finish_reason_value is not None
                        else None
                    )
                    completed = True
                    if self.session_manager is not None:
                        try:
                            self.session_manager.save_completed_turns()
                        except SessionError as error:
                            self.notify(
                                f"Session could not be saved: {error}",
                                severity="error",
                            )
                elif event.type == AgentEventType.AGENT_ERROR:
                    await assistant_message.show_error(
                        str(
                            event.data.get("error")
                            or "The request failed without an error message."
                        )
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
                    self.query_one(StatusBar).set_usage(usage)
        except asyncio.CancelledError:
            await assistant_message.show_cancelled()
            outcome = "stopped"
        except Exception as error:  # noqa: BLE001 - render unexpected worker failures
            await assistant_message.show_error(str(error))
            outcome = "error"
        finally:
            if self._active_assistant is assistant_message:
                self._active_assistant = None
            self._set_busy(False)
            self._scroll_to_latest()
            self.query_one(PromptInput).focus()

    async def _request_tool_approval(
        self,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        if request.tool_name in self._always_approved:
            card = self._tool_cards.get(request.call_id)
            if card is not None:
                card.set_state("running")
            return ApprovalDecision.APPROVED

        future: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        card = self._tool_cards.get(request.call_id)
        if card is None:
            card = ToolCallCard(
                request.call_id,
                request.tool_name,
                request.arguments,
                state="awaiting-approval",
            )
            self._tool_cards[request.call_id] = card
            transcript = self.query_one("#transcript", VerticalScroll)
            before = (
                self._active_assistant
                if self._active_assistant is not None
                and self._active_assistant.is_mounted
                else None
            )
            await transcript.mount(card, before=before)
        else:
            card.set_awaiting_approval(request.arguments)

        self._pending_approval = _PendingApproval(
            request.call_id,
            request.tool_name,
            future,
            card,
        )

        card.query_one(".approval-approve", Button).focus(scroll_visible=False)
        self.call_after_refresh(
            card.scroll_visible,
            animate=False,
            top=True,
            immediate=True,
        )

        try:
            return await future
        finally:
            self._clear_pending_approval()

    def _resolve_pending_approval(
        self,
        decision: ApprovalDecision,
        *,
        always: bool = False,
    ) -> None:
        pending = self._pending_approval
        if pending is None or pending.future.done():
            return

        if always:
            self._always_approved.add(pending.tool_name)
        pending.card.set_state(
            "running" if decision is ApprovalDecision.APPROVED else "rejected"
        )
        pending.future.set_result(decision)
        self._clear_pending_approval()

    def _clear_pending_approval(self) -> None:
        self._pending_approval = None

    async def _show_tool_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: str,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        if assistant_message.content_text:
            assistant_message.finish_segment()
        else:
            await assistant_message.remove()

        card = ToolCallCard(
            call_id,
            tool_name,
            arguments,
            state="running",
        )
        self._tool_cards[call_id] = card
        transcript = self.query_one("#transcript", VerticalScroll)
        next_assistant = ChatMessage(
            "assistant",
            model_name=self._model_name,
        )
        await transcript.mount(card, next_assistant)
        self._active_assistant = next_assistant
        self._scroll_to_latest()
        return next_assistant

    def _finish_tool_call(
        self,
        *,
        call_id: str,
        status: str,
        content: str,
    ) -> None:
        card = self._tool_cards.get(call_id)
        if card is not None:
            card.finish(status, content)
            self._scroll_to_latest()

    def _reject_tool_call(self, *, call_id: str, content: str) -> None:
        card = self._tool_cards.get(call_id)
        if card is not None:
            card.reject(content)
            self._scroll_to_latest()

    def _scroll_to_latest(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one(Composer).set_busy(busy)

    async def action_new_chat(self) -> None:
        active_worker = self._active_worker
        if active_worker is not None and active_worker.is_running:
            active_worker.cancel()
            try:
                await active_worker.wait()
            except WorkerCancelled:
                pass
        if self.session_manager is None:
            self.agent.reset()
        else:
            self.session_manager.create_session()
        self._clear_pending_approval()
        await self.query(".chat-message").remove()
        await self.query(".tool-call-card").remove()
        self._tool_cards.clear()
        self._active_assistant = None
        self.query_one("#empty-state", EmptyState).styles.display = "block"
        self._set_busy(False)
        self.query_one(StatusBar).reset()
        self.screen.add_class("empty-chat")
        self.query_one(PromptInput).focus()

    def action_cancel_response(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        else:
            self.query_one(PromptInput).focus()
