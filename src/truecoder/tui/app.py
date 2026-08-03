from __future__ import annotations

import asyncio
import json
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
from truecoder.agent.approval import (
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalScope,
)
from truecoder.agent.events import AgentEventType
from truecoder.agent.messages import ModelMessage
from truecoder.client.response import TokenUsage
from truecoder.execution.context import workspace_id_for
from truecoder.session import SessionError, SessionManager
from truecoder.session.models import SessionRecord
from truecoder.tui.sessions import (
    DeleteSessionScreen,
    RenameSessionScreen,
    SessionAction,
    SessionManagerScreen,
)
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
    request: ApprovalRequest
    future: asyncio.Future[ApprovalResponse]
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
        Binding("ctrl+p", "manage_sessions", "Sessions", show=False, priority=True),
        Binding("escape", "cancel_response", "Stop", show=False),
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
        if session_manager is not None and isinstance(
            session_manager.project_root,
            Path,
        ):
            workspace_id = workspace_id_for(session_manager.project_root)
            self.agent.set_approval_identity_provider(
                lambda: ApprovalIdentity(
                    session_id=session_manager.active_session.session_id,
                    workspace_id=workspace_id,
                )
            )
        self._busy = False
        self._active_worker: Worker[None] | None = None
        self._active_assistant: ChatMessage | None = None
        self._pending_approval: _PendingApproval | None = None
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

    async def on_mount(self) -> None:
        self.screen.add_class("empty-chat")
        await self.agent.initialize_execution()
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

    @on(Button.Pressed, ".approval-once")
    def approve_pending_tool_once(self) -> None:
        self._resolve_pending_approval(ApprovalScope.ONCE)

    @on(Button.Pressed, ".approval-session")
    def approve_pending_tool_for_session(self) -> None:
        self._resolve_pending_approval(ApprovalScope.SESSION)

    @on(Button.Pressed, ".approval-workspace")
    def approve_pending_tool_for_workspace(self) -> None:
        self._resolve_pending_approval(ApprovalScope.WORKSPACE)

    @on(Button.Pressed, ".approval-reject")
    def reject_pending_tool(self) -> None:
        self._resolve_pending_approval(None)

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
                        raw_scopes = event.data.get("allowed_scopes")
                        allowed_scopes = (
                            tuple(
                                str(scope)
                                for scope in raw_scopes
                                if isinstance(scope, str)
                            )
                            if isinstance(raw_scopes, (list, tuple))
                            else ("once",)
                        )
                        card.set_awaiting_approval(
                            arguments,
                            allowed_scopes=allowed_scopes,
                        )
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
    ) -> ApprovalResponse:
        future: asyncio.Future[ApprovalResponse] = (
            asyncio.get_running_loop().create_future()
        )
        card = self._tool_cards.get(request.call_id)
        if card is None:
            card = ToolCallCard(
                request.call_id,
                request.tool_name,
                request.arguments,
                state="awaiting-approval",
                allowed_approval_scopes=tuple(
                    scope.value for scope in request.allowed_scopes
                ),
            )
            card.approval_details = self._approval_detail_rows(request)
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
            card.set_awaiting_approval(
                request.arguments,
                allowed_scopes=tuple(
                    scope.value for scope in request.allowed_scopes
                ),
                approval_details=self._approval_detail_rows(request),
            )

        self._pending_approval = _PendingApproval(
            request,
            future,
            card,
        )

        card.query_one(".approval-once", Button).focus(scroll_visible=False)
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
        scope: ApprovalScope | None,
    ) -> None:
        pending = self._pending_approval
        if pending is None or pending.future.done():
            return

        if scope is not None and scope not in pending.request.allowed_scopes:
            return
        response = (
            ApprovalResponse.reject()
            if scope is None
            else ApprovalResponse.approve(scope)
        )
        pending.card.set_state(
            "running" if scope is not None else "rejected"
        )
        pending.future.set_result(response)
        self._clear_pending_approval()

    def _clear_pending_approval(self) -> None:
        self._pending_approval = None

    @staticmethod
    def _approval_detail_rows(
        request: ApprovalRequest,
    ) -> tuple[tuple[str, str], ...]:
        execution = request.execution
        if execution is None:
            return ()

        limits = execution.effective_limits
        capabilities = execution.capabilities

        def optional_limit(value: object, suffix: str = "") -> str:
            return "not requested" if value is None else f"{value}{suffix}"

        return (
            ("Command", execution.command_display),
            ("Directory", str(execution.working_directory)),
            ("Backend", execution.backend),
            ("Risk", execution.risk.value),
            ("Mode", execution.request.mode),
            ("Shell", execution.request.shell_kind),
            (
                "Network",
                "allowed" if execution.request.network_access else "denied",
            ),
            ("Filesystem", execution.request.filesystem_mode),
            ("Timeout", f"{limits.timeout_seconds}s"),
            ("Termination grace", f"{limits.termination_grace_seconds}s"),
            ("Output limit", f"{limits.max_output_bytes} bytes"),
            ("Return limit", f"{limits.max_return_bytes} bytes"),
            ("Memory limit", optional_limit(limits.memory_bytes, " bytes")),
            ("CPU limit", optional_limit(limits.cpu_seconds, "s")),
            ("Process limit", optional_limit(limits.max_processes)),
            ("Filesystem isolation", capabilities.filesystem_isolation),
            ("Network isolation", capabilities.network_isolation),
            ("Memory enforcement", capabilities.memory_limits),
            ("CPU enforcement", capabilities.cpu_limits),
            ("Process enforcement", capabilities.process_limits),
            ("Timeout enforcement", capabilities.timeout_enforcement),
            ("Cancellation", capabilities.cancellation),
            (
                "Supported modes",
                ", ".join(capabilities.supported_execution_modes),
            ),
            (
                "Supported filesystems",
                ", ".join(capabilities.supported_filesystem_modes),
            ),
            (
                "Supported shells",
                ", ".join(capabilities.supported_shells) or "none",
            ),
        )

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

    def action_manage_sessions(self) -> None:
        if self.session_manager is None:
            self.notify("Session storage is not configured.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before managing sessions.",
                severity="warning",
            )
            return
        self.push_screen(
            SessionManagerScreen(
                self.session_manager.list_sessions(),
                self.session_manager.active_session.session_id,
            ),
            self._handle_session_action,
        )

    async def _handle_session_action(
        self,
        action: SessionAction | None,
    ) -> None:
        if action is None or self.session_manager is None:
            return
        try:
            if action.kind == "new":
                self.session_manager.create_session()
                await self._render_active_session()
            elif action.kind == "switch" and action.session_id is not None:
                record = self.session_manager.switch_session(action.session_id)
                await self._render_session(record)
            elif action.kind == "rename" and action.session_id is not None:
                summary = self._session_summary(action.session_id)
                self.push_screen(
                    RenameSessionScreen(summary.title),
                    lambda title: self._finish_session_rename(
                        action.session_id,
                        title,
                    ),
                )
            elif action.kind == "delete" and action.session_id is not None:
                summary = self._session_summary(action.session_id)
                self.push_screen(
                    DeleteSessionScreen(summary.title),
                    lambda confirmed: self._finish_session_delete(
                        action.session_id,
                        confirmed,
                    ),
                )
        except SessionError as error:
            self.notify(f"Session operation failed: {error}", severity="error")

    def _session_summary(self, session_id: str):
        if self.session_manager is None:
            raise RuntimeError("Session storage is not configured.")
        return next(
            summary
            for summary in self.session_manager.list_sessions()
            if summary.session_id == session_id
        )

    def _finish_session_rename(
        self,
        session_id: str,
        title: str | None,
    ) -> None:
        if title is None or self.session_manager is None:
            return
        try:
            self.session_manager.rename_session(session_id, title)
        except (SessionError, ValueError) as error:
            self.notify(f"Session could not be renamed: {error}", severity="error")

    async def _finish_session_delete(
        self,
        session_id: str,
        confirmed: bool,
    ) -> None:
        if not confirmed or self.session_manager is None:
            return
        deleting_active = session_id == self.session_manager.active_session.session_id
        try:
            self.session_manager.delete_session(session_id)
            if deleting_active:
                await self._render_active_session()
        except SessionError as error:
            self.notify(f"Session could not be deleted: {error}", severity="error")

    async def _render_active_session(self) -> None:
        if self.session_manager is None:
            return
        record = SessionRecord(
            summary=self.session_manager.active_session,
            completed_turns=tuple(
                tuple(turn) for turn in self.agent.state.completed_turns
            ),
        )
        await self._render_session(record)

    async def _render_session(self, record: SessionRecord) -> None:
        await self.query(".chat-message").remove()
        await self.query(".tool-call-card").remove()
        self._tool_cards.clear()
        self._active_assistant = None
        transcript = self.query_one("#transcript", VerticalScroll)
        cards: dict[str, ToolCallCard] = {}

        for turn in record.completed_turns:
            for message in turn:
                role = message["role"]
                if role == "user":
                    await transcript.mount(
                        ChatMessage(
                            "user",
                            message["content"],
                            model_name=self._model_name,
                        )
                    )
                elif role == "assistant" and "tool_calls" in message:
                    if message["content"]:
                        segment = ChatMessage(
                            "assistant",
                            message["content"],
                            model_name=self._model_name,
                        )
                        await transcript.mount(segment)
                        segment.finish_segment()
                    for raw_call in message["tool_calls"]:
                        function = raw_call["function"]
                        card = ToolCallCard(
                            raw_call["id"],
                            function["name"],
                            function["arguments"],
                            state="running",
                        )
                        cards[raw_call["id"]] = card
                        self._tool_cards[raw_call["id"]] = card
                        await transcript.mount(card)
                elif role == "tool":
                    card = cards.get(message["tool_call_id"])
                    if card is not None:
                        try:
                            payload = json.loads(message["content"])
                        except json.JSONDecodeError:
                            payload = {}
                        status = (
                            "approval_rejected"
                            if payload.get("error_code") == "approval_rejected"
                            else str(payload.get("status", "error"))
                        )
                        card.restore_result(status, message["content"])
                elif role == "assistant":
                    restored = ChatMessage(
                        "assistant",
                        message["content"],
                        model_name=self._model_name,
                    )
                    await transcript.mount(restored)
                    restored.restore()

        has_history = bool(record.completed_turns)
        self.query_one("#empty-state", EmptyState).styles.display = (
            "none" if has_history else "block"
        )
        self.screen.set_class(not has_history, "empty-chat")
        status_bar = self.query_one(StatusBar)
        status_bar.reset()
        status_bar.set_conversation_active(has_history)
        self._set_busy(False)
        self._scroll_to_latest()
        self.query_one(PromptInput).focus()

    def action_cancel_response(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        else:
            self.query_one(PromptInput).focus()
