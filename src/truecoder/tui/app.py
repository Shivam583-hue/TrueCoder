from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import ClassVar, Final

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button
from textual.worker import Worker, WorkerCancelled, WorkerFailed

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
from truecoder.execution.models import ExecutionLifecycleEvent
from truecoder.planning import Plan
from truecoder.session import SessionError, SessionManager
from truecoder.session.models import SessionRecord
from truecoder.tools.builtin import UpdatePlanTool
from truecoder.tui.audit_view import AuditViewerScreen, audit_row_from
from truecoder.tui.changes import WorkspaceChangesScreen
from truecoder.tui.checkpoints import (
    CheckpointBrowserScreen,
    RestoreCheckpointScreen,
)
from truecoder.tui.execution_health import (
    ExecutionHealthScreen,
    health_failure_message,
)
from truecoder.tui.execution_view import (
    compact_approval_rows,
    full_approval_rows,
    is_terminal_stage,
)
from truecoder.tui.memory import MemoryAction, MemoryBrowserScreen
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
    ExecutionCard,
    PlanCard,
    PromptInput,
    StatusBar,
    ToolCallCard,
)

SHUTDOWN_DRAIN_SECONDS: Final = 5.0


@dataclass
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalResponse]
    card: ToolCallCard


class ExecutionStageMessage(Message):
    def __init__(self, event: ExecutionLifecycleEvent) -> None:
        self.event = event
        super().__init__()


class ExecutionOutputMessage(Message):
    def __init__(self, execution_id: str, stream: str, text: str) -> None:
        self.execution_id = execution_id
        self.stream = stream
        self.text = text
        super().__init__()


class _AppEventSink:
    def __init__(self, app: App[None]) -> None:
        self._app = app

    async def publish(self, event: ExecutionLifecycleEvent) -> None:
        self._app.post_message(ExecutionStageMessage(event))


class _AppPreviewSink:
    def __init__(self, app: App[None]) -> None:
        self._app = app

    async def publish_bounded(
        self,
        execution_id: str,
        stream: str,
        text: str,
    ) -> None:
        self._app.post_message(
            ExecutionOutputMessage(execution_id, stream, text)
        )


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
        Binding("ctrl+a", "manage_audit", "Audit", show=False, priority=True),
        Binding("ctrl+e", "execution_health", "Execution", show=False, priority=True),
        Binding("ctrl+r", "manage_checkpoints", "Checkpoints", show=False, priority=True),
        Binding("ctrl+d", "review_changes", "Changes", show=False, priority=True),
        Binding("ctrl+n", "manage_memory", "Memory", show=False, priority=True),
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
        self._execution_cards: dict[str, ExecutionCard] = {}
        self._plan_card: PlanCard | None = None
        self._plan_calls: dict[str, str] = {}
        self._model_name = "model not configured"
        self.agent.set_execution_sinks(
            event_sink=_AppEventSink(self),
            preview_sink=_AppPreviewSink(self),
        )

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
        runtime = await self.agent.initialize_execution()
        if runtime is not None:
            failure = health_failure_message(runtime.health)
            self.query_one(StatusBar).set_execution_health(failure)
            if failure is not None:
                self.notify(
                    f"Shell execution unavailable: {failure}.",
                    severity="warning",
                    timeout=8,
                )
        self.query_one(PromptInput).focus()

    async def on_unmount(self) -> None:
        self._reject_pending_approval_for_shutdown()
        await self._cancel_active_executions(reason="application shutting down")
        await self._drain_active_worker()
        try:
            await self.agent.close()
        finally:
            if self.session_manager is not None:
                self.session_manager.close()

    def _reject_pending_approval_for_shutdown(self) -> None:
        pending = self._pending_approval
        if pending is None or pending.future.done():
            self._clear_pending_approval()
            return
        pending.future.set_result(ApprovalResponse.reject())
        self._clear_pending_approval()

    async def _cancel_active_executions(self, *, reason: str) -> bool:
        service = self._execution_service()
        if service is None:
            return False
        try:
            execution_ids = await service.registry.active_execution_ids()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            return False
        if not execution_ids:
            return False
        for execution_id in execution_ids:
            card = self._execution_cards.get(execution_id)
            if card is not None:
                card.mark_cancelling()
            try:
                await service.cancel(execution_id, reason=reason)
            except Exception:  # noqa: BLE001, S110 - cancellation is a request
                pass
        return True

    async def _drain_active_worker(self) -> None:
        worker = self._active_worker
        if worker is None or not worker.is_running:
            return
        worker.cancel()
        try:
            await asyncio.wait_for(worker.wait(), timeout=SHUTDOWN_DRAIN_SECONDS)
        except (
            TimeoutError,
            WorkerCancelled,
            WorkerFailed,
            asyncio.CancelledError,
        ):
            return

    def _execution_service(self):
        runtime = self.agent.execution_runtime
        return runtime.service if runtime is not None else None

    @on(PromptInput.Submitted)
    async def submit_from_keyboard(self, event: PromptInput.Submitted) -> None:
        await self._submit_prompt(event.value)

    @on(ExecutionStageMessage)
    async def advance_execution_card(self, message: ExecutionStageMessage) -> None:
        event = message.event
        details = dict(event.details)
        card = await self._execution_card(
            event.execution_id,
            command=details.get("command"),
        )
        if card is None:
            return
        card.apply_stage(event.stage, event.message)
        if is_terminal_stage(event.stage):
            self._execution_cards.pop(event.execution_id, None)
        self._scroll_to_latest()

    @on(ExecutionOutputMessage)
    def append_execution_output(self, message: ExecutionOutputMessage) -> None:
        card = self._execution_cards.get(message.execution_id)
        if card is None:
            return
        card.append_output(message.stream, message.text)

    @on(ExecutionCard.CancelRequested)
    async def cancel_one_execution(
        self,
        message: ExecutionCard.CancelRequested,
    ) -> None:
        message.stop()
        await self._cancel_execution(
            message.execution_id,
            reason="cancelled from the interface",
        )

    async def _cancel_execution(self, execution_id: str, *, reason: str) -> None:
        service = self._execution_service()
        if service is None:
            return
        card = self._execution_cards.get(execution_id)
        if card is not None:
            card.mark_cancelling()
        try:
            await service.cancel(execution_id, reason=reason)
        except Exception as error:  # noqa: BLE001 - cancellation is a request
            self.notify(f"Cancellation failed: {error}", severity="warning")

    async def _execution_card(
        self,
        execution_id: str,
        *,
        command: str | None = None,
    ) -> ExecutionCard | None:
        card = self._execution_cards.get(execution_id)
        if card is not None:
            if command:
                card.set_command(command)
            return card
        if not self.is_mounted:
            return None

        card = ExecutionCard(execution_id, command or "shell command")
        self._execution_cards[execution_id] = card
        transcript = self.query_one("#transcript", VerticalScroll)
        before = (
            self._active_assistant
            if self._active_assistant is not None
            and self._active_assistant.is_mounted
            else None
        )
        await transcript.mount(card, before=before)
        return card

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
                    call_id = str(event.data.get("call_id", ""))
                    tool_name = str(event.data.get("name", "tool"))
                    arguments = str(event.data.get("arguments", "{}"))
                    if tool_name == UpdatePlanTool.name:
                        self._plan_calls[call_id] = arguments
                    else:
                        assistant_message = await self._show_tool_call(
                            call_id=call_id,
                            tool_name=tool_name,
                            arguments=arguments,
                            assistant_message=assistant_message,
                        )
                elif event.type == AgentEventType.APPROVAL_REQUESTED:
                    # The card is moved into approval by _request_tool_approval,
                    # which the agent always calls straight after this event.
                    # Showing the controls here would arm them before anything
                    # is listening, and a click landing in that window is lost.
                    self._scroll_to_latest()
                elif event.type == AgentEventType.TOOL_RESULT:
                    call_id = str(event.data.get("call_id", ""))
                    status = str(event.data.get("status", "error"))
                    content = str(event.data.get("content", ""))
                    if call_id in self._plan_calls:
                        assistant_message = await self._resolve_plan_call(
                            call_id=call_id,
                            status=status,
                            content=content,
                            assistant_message=assistant_message,
                        )
                    else:
                        self._finish_tool_call(
                            call_id=call_id,
                            status=status,
                            content=content,
                        )
                elif event.type == AgentEventType.TOOL_REJECTED:
                    call_id = str(event.data.get("call_id", ""))
                    content = str(event.data.get("content", ""))
                    if call_id in self._plan_calls:
                        assistant_message = await self._resolve_plan_call(
                            call_id=call_id,
                            status="rejected",
                            content=content,
                            assistant_message=assistant_message,
                        )
                    else:
                        self._reject_tool_call(call_id=call_id, content=content)
                elif event.type == AgentEventType.PROGRESS_STALLED:
                    self.notify(
                        "The agent was repeating itself and was asked to answer "
                        "with what it had.",
                        severity="warning",
                        timeout=8,
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
                    self._announce_hooks()
                    await self._announce_changes()
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
        allowed_scopes = tuple(scope.value for scope in request.allowed_scopes)
        card = self._tool_cards.get(request.call_id)
        restored = card is not None

        if card is None:
            card = ToolCallCard(
                request.call_id,
                request.tool_name,
                request.arguments,
                state="awaiting-approval",
                allowed_approval_scopes=allowed_scopes,
                mutation=request.mutation,
            )
            card.approval_details = self._approval_detail_rows(request)
            self._tool_cards[request.call_id] = card

        # Registered before the controls can be reached, so a decision can
        # never arrive while _resolve_pending_approval would discard it.
        self._pending_approval = _PendingApproval(
            request,
            future,
            card,
        )

        if restored:
            card.set_awaiting_approval(
                request.arguments,
                allowed_scopes=allowed_scopes,
                approval_details=self._approval_detail_rows(request),
                mutation=request.mutation,
            )
        else:
            transcript = self.query_one("#transcript", VerticalScroll)
            before = (
                self._active_assistant
                if self._active_assistant is not None
                and self._active_assistant.is_mounted
                else None
            )
            await transcript.mount(card, before=before)

        self._attach_execution_approval(request)

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
        return compact_approval_rows(
            execution,
            tuple(scope.value for scope in request.allowed_scopes),
        )

    def _attach_execution_approval(self, request: ApprovalRequest) -> None:
        execution = request.execution
        if execution is None:
            return
        card = self._execution_cards.get(execution.execution_id)
        if card is None:
            return
        card.set_command(execution.command_display)
        card.call_id = request.call_id
        card.set_approval(
            compact_approval_rows(
                execution,
                tuple(scope.value for scope in request.allowed_scopes),
            ),
            full_approval_rows(execution),
        )

    async def _mount_after_assistant(
        self,
        widget: ToolCallCard | PlanCard,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        if assistant_message.content_text:
            assistant_message.finish_segment()
        else:
            await assistant_message.remove()

        transcript = self.query_one("#transcript", VerticalScroll)
        next_assistant = ChatMessage(
            "assistant",
            model_name=self._model_name,
        )
        await transcript.mount(widget, next_assistant)
        self._active_assistant = next_assistant
        self._scroll_to_latest()
        return next_assistant

    async def _show_tool_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: str,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        card = ToolCallCard(
            call_id,
            tool_name,
            arguments,
            state="running",
        )
        self._tool_cards[call_id] = card
        return await self._mount_after_assistant(card, assistant_message)

    async def _resolve_plan_call(
        self,
        *,
        call_id: str,
        status: str,
        content: str,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        arguments = self._plan_calls.pop(call_id, "{}")
        plan = self._current_plan()

        if status != "success" or plan is None:
            next_assistant = await self._show_tool_call(
                call_id=call_id,
                tool_name=UpdatePlanTool.name,
                arguments=arguments,
                assistant_message=assistant_message,
            )
            self._finish_tool_call(
                call_id=call_id,
                status=status,
                content=content,
            )
            return next_assistant

        self.query_one(StatusBar).set_plan(plan)

        if self._plan_card is None:
            self._plan_card = PlanCard(plan)
            return await self._mount_after_assistant(
                self._plan_card,
                assistant_message,
            )

        self._plan_card.update_plan(plan)
        self._scroll_to_latest()
        return assistant_message

    def _current_plan(self) -> Plan | None:
        store = self.agent.plan_store
        return None if store is None else store.current

    def _clear_plan(self) -> None:
        self._plan_card = None
        self._plan_calls.clear()
        if self.agent.plan_store is not None:
            self.agent.plan_store.clear()

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
        try:
            self.query_one(Composer).set_busy(busy)
        except NoMatches:
            return

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
        await self.query(".execution-card").remove()
        await self.query(".plan-card").remove()
        self._tool_cards.clear()
        self._execution_cards.clear()
        self._clear_plan()
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

    async def action_manage_audit(self) -> None:
        runtime = self.agent.execution_runtime
        audit = runtime.audit if runtime is not None else None
        if audit is None:
            self.notify("Execution audit is unavailable.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before viewing the audit.",
                severity="warning",
            )
            return
        project_root = (
            self.session_manager.project_root
            if self.session_manager is not None
            else Path.cwd().resolve()
        )
        try:
            snapshots = await audit.list_runs(
                workspace_id=workspace_id_for(project_root),
            )
            events = await asyncio.gather(
                *(audit.get_events(item.record.run_id) for item in snapshots)
            )
        except Exception as error:  # noqa: BLE001
            self.notify(f"Audit could not be read: {error}", severity="error")
            return
        self.push_screen(
            AuditViewerScreen(
                tuple(
                    audit_row_from(snapshot, run_events)
                    for snapshot, run_events in zip(
                        snapshots,
                        events,
                        strict=True,
                    )
                )
            )
        )

    async def action_review_changes(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            self.notify("Checkpoints are not configured.", severity="warning")
            return
        if isinstance(self.screen, WorkspaceChangesScreen):
            return
        if self._busy:
            self.notify(
                "Stop the current response before reviewing changes.",
                severity="warning",
            )
            return

        try:
            reason = await service.unavailable_reason()
            changes = None if reason is not None else await self.agent.turn_changes()
        except Exception as error:  # noqa: BLE001 - report a failed comparison
            self.notify(f"Changes could not be read: {error}", severity="error")
            return

        self.push_screen(
            WorkspaceChangesScreen(changes, unavailable_reason=reason)
        )

    def _announce_hooks(self) -> None:
        failures = [
            outcome for outcome in self.agent.hook_outcomes if not outcome.ok
        ]
        if not failures:
            return

        described = ", ".join(outcome.summary for outcome in failures)
        self.notify(f"Hook: {described}", severity="warning", timeout=10)

    async def _announce_changes(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            return

        try:
            changes = await self.agent.turn_changes()
        except Exception:  # noqa: BLE001 - a summary never disturbs a finished turn
            return

        if changes is None or changes.is_empty:
            return

        self.notify(
            f"{changes.summary}. Press ctrl+d to review.",
            timeout=8,
        )

    def action_manage_memory(self) -> None:
        store = self.agent.memory_store
        if store is None:
            self.notify("Memory is not configured.", severity="warning")
            return

        try:
            entries = store.entries()
        except Exception as error:  # noqa: BLE001 - report an unreadable store
            self.notify(f"Memory could not be read: {error}", severity="error")
            return

        self.push_screen(
            MemoryBrowserScreen(entries),
            self._handle_memory_action,
        )

    def _handle_memory_action(self, action: MemoryAction | None) -> None:
        store = self.agent.memory_store
        if action is None or store is None:
            return

        try:
            if action.kind == "clear":
                removed = store.clear()
                self.notify(f"Forgot {removed} note(s).")
                return
            if action.entry_id is not None and store.forget(action.entry_id):
                self.notify("Forgot that note.")
        except Exception as error:  # noqa: BLE001 - report a failed write
            self.notify(f"Memory could not be changed: {error}", severity="error")

    async def action_manage_checkpoints(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            self.notify("Checkpoints are not configured.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before restoring a checkpoint.",
                severity="warning",
            )
            return

        reason = await service.unavailable_reason()
        checkpoints = () if reason is not None else await service.list()
        self.push_screen(
            CheckpointBrowserScreen(checkpoints, unavailable_reason=reason),
            self._handle_checkpoint_choice,
        )

    async def _handle_checkpoint_choice(self, checkpoint_id: str | None) -> None:
        service = self.agent.checkpoints
        if checkpoint_id is None or service is None:
            return

        checkpoint = await service.find(checkpoint_id)
        if checkpoint is None:
            self.notify("That checkpoint no longer exists.", severity="warning")
            return

        removals = await service.preview(checkpoint)
        self.push_screen(
            RestoreCheckpointScreen(checkpoint, removals),
            lambda confirmed: self._finish_restore(checkpoint_id, confirmed),
        )

    def _finish_restore(self, checkpoint_id: str, confirmed: bool | None) -> None:
        if confirmed:
            self.run_worker(self._restore_checkpoint(checkpoint_id))

    async def _restore_checkpoint(self, checkpoint_id: str) -> None:
        service = self.agent.checkpoints
        if service is None:
            return

        try:
            outcome = await service.restore(checkpoint_id)
        except Exception as error:  # noqa: BLE001 - report a failed restore
            self.notify(f"Restore failed: {error}", severity="error", timeout=10)
            return

        self.agent.turn_checkpoint = None
        self.notify(outcome.summary, timeout=10)

    def action_execution_health(self) -> None:
        runtime = self.agent.execution_runtime
        if runtime is None:
            self.notify("Execution subsystem is not configured.", severity="warning")
            return
        self.push_screen(ExecutionHealthScreen(runtime.health))

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
        self.agent.turn_checkpoint = None
        await self.query(".chat-message").remove()
        await self.query(".tool-call-card").remove()
        await self.query(".execution-card").remove()
        await self.query(".plan-card").remove()
        self._tool_cards.clear()
        self._execution_cards.clear()
        self._clear_plan()
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
                        if function["name"] == UpdatePlanTool.name:
                            continue
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

    async def action_cancel_response(self) -> None:
        if self._pending_approval is not None:
            self._resolve_pending_approval(None)
            return
        if await self._cancel_active_executions(reason="stopped by the user"):
            return
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        else:
            self.query_one(PromptInput).focus()
