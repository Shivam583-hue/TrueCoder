from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from truecoder.agent.approval import (
    ApprovalDecision,
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalService,
    reject_all_tool_calls,
)
from truecoder.agent.context import ContextBuilder
from truecoder.agent.events import AgentEvent
from truecoder.agent.messages import ModelMessage
from truecoder.agent.project_instructions import (
    find_project_root,
    load_project_instructions,
)
from truecoder.agent.state import AgentState
from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage
from truecoder.execution.bootstrap import (
    ExecutionBootstrapConfig,
    ExecutionRuntime,
    bootstrap_execution,
)
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.context import ExecutionContextFactory, workspace_id_for
from truecoder.execution.events import ExecutionEventSink
from truecoder.execution.runner import PreviewSink
from truecoder.session import (
    SessionManager,
    SQLiteSessionStore,
    default_session_database_path,
)
from truecoder.tools import ToolExecutor, ToolInvocationContext, serialize_tool_result
from truecoder.tools.base import (
    ToolApproval,
    ToolCall,
    ToolResult,
)
from truecoder.tools.builtin import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    ShellDefaults,
    ShellTool,
    WriteFileTool,
)
from truecoder.tools.registry import ToolRegistry

DEFAULT_MAX_ITERATIONS = 25
ApprovalIdentityProvider = Callable[[], ApprovalIdentity]
CompatibleApprovalHandler = Callable[
    [ApprovalRequest],
    Awaitable[ApprovalResponse | ApprovalDecision],
]


class Agent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        state: AgentState | None = None,
        context_builder: ContextBuilder | None = None,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        approval_handler: CompatibleApprovalHandler | None = None,
        approval_service: ApprovalService | None = None,
        approval_identity_provider: ApprovalIdentityProvider | None = None,
        project_root: Path | None = None,
        execution_context_factory: ExecutionContextFactory | None = None,
        execution_bootstrap_config: ExecutionBootstrapConfig | None = None,
    ) -> None:
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise TypeError("max_iterations must be an integer.")

        if max_iterations < 1:
            raise ValueError("max_iterations must be at least one.")
        if approval_handler is not None and approval_service is not None:
            raise ValueError(
                "approval_handler and approval_service cannot both be supplied."
            )
        if approval_identity_provider is not None and not callable(
            approval_identity_provider
        ):
            raise TypeError("approval_identity_provider must be callable.")
        if execution_context_factory is not None and not isinstance(
            execution_context_factory,
            ExecutionContextFactory,
        ):
            raise TypeError(
                "execution_context_factory must be an ExecutionContextFactory."
            )
        if execution_bootstrap_config is not None and not isinstance(
            execution_bootstrap_config,
            ExecutionBootstrapConfig,
        ):
            raise TypeError(
                "execution_bootstrap_config must be an ExecutionBootstrapConfig."
            )

        root = project_root or Path.cwd()
        try:
            self._project_root = root.resolve(strict=True)
        except OSError as error:
            raise ValueError("project_root must exist and be accessible.") from error
        if not self._project_root.is_dir():
            raise ValueError("project_root must be a directory.")

        self.llm_client = llm_client if llm_client is not None else LLMClient()
        self.state = state if state is not None else AgentState()
        self.context_builder = (
            context_builder
            if context_builder is not None
            else ContextBuilder.from_environment()
        )
        self.tool_registry = (
            tool_registry if tool_registry is not None else ToolRegistry()
        )
        self.tool_executor = ToolExecutor(self.tool_registry)
        self.max_iterations = max_iterations
        self._execution_context_factory = (
            execution_context_factory or ExecutionContextFactory()
        )
        self._execution_bootstrap_config = execution_bootstrap_config
        self._execution_runtime: ExecutionRuntime | None = None
        self._execution_initialized = False
        self._default_approval_identity = ApprovalIdentity(
            session_id=f"session_{uuid.uuid4().hex}",
            workspace_id=workspace_id_for(self._project_root),
        )
        self._approval_identity_provider = (
            approval_identity_provider
            if approval_identity_provider is not None
            else lambda: self._default_approval_identity
        )
        self._approval_handler: CompatibleApprovalHandler = reject_all_tool_calls
        self.approval_service = approval_service or ApprovalService(
            self._invoke_approval_handler
        )
        if approval_handler is not None:
            self.approval_handler = approval_handler

    @property
    def approval_handler(self) -> CompatibleApprovalHandler:
        return self._approval_handler

    @approval_handler.setter
    def approval_handler(self, handler: CompatibleApprovalHandler) -> None:
        if not callable(handler):
            raise TypeError("approval_handler must be callable.")
        self._approval_handler = handler
        self.approval_service.handler = self._invoke_approval_handler

    def set_approval_identity_provider(
        self,
        provider: ApprovalIdentityProvider,
    ) -> None:
        if not callable(provider):
            raise TypeError("approval identity provider must be callable.")
        self._approval_identity_provider = provider

    @property
    def messages(self) -> list[ModelMessage]:
        return self.state.messages

    @property
    def execution_runtime(self) -> ExecutionRuntime | None:
        return self._execution_runtime

    def set_execution_sinks(
        self,
        *,
        event_sink: ExecutionEventSink | None = None,
        preview_sink: PreviewSink | None = None,
    ) -> None:
        """Attach presentation sinks before execution is initialized.

        Sinks are presentation only. They never gate, delay, or fail a run,
        so attaching them after initialization would silently observe nothing
        and is refused instead.
        """
        if self._execution_initialized:
            raise RuntimeError(
                "execution sinks must be attached before initialization."
            )
        if event_sink is not None and not isinstance(
            event_sink,
            ExecutionEventSink,
        ):
            raise TypeError("event_sink must implement ExecutionEventSink.")
        if preview_sink is not None and not hasattr(
            preview_sink,
            "publish_bounded",
        ):
            raise TypeError("preview_sink must provide publish_bounded.")
        if self._execution_bootstrap_config is None:
            return
        self._execution_bootstrap_config = replace(
            self._execution_bootstrap_config,
            event_sink=event_sink,
            preview_sink=preview_sink,
        )

    async def initialize_execution(self) -> ExecutionRuntime | None:
        if self._execution_initialized:
            return self._execution_runtime
        if self._execution_bootstrap_config is None:
            self._execution_initialized = True
            return None

        runtime = await bootstrap_execution(
            self.approval_service,
            config=self._execution_bootstrap_config,
        )
        self._execution_runtime = runtime
        self._execution_initialized = True
        if runtime.shell_available and runtime.service is not None:
            if "shell" not in self.tool_registry:
                self.tool_registry.register(
                    ShellTool(
                        self._project_root,
                        runtime.service,
                        ShellDefaults(
                            self._execution_bootstrap_config.policy_config.limit_ceiling
                        ),
                    )
                )
            self.context_builder.enable_shell_tool()
        return runtime

    async def run(self, prompt: str) -> AsyncGenerator[AgentEvent, None]:
        prompt = prompt.strip()
        if not prompt:
            yield AgentEvent.agent_error("The prompt cannot be empty.")
            return
        await self.initialize_execution()
        try:
            self.state.begin_turn(prompt)
        except (ValueError, RuntimeError) as error:
            yield AgentEvent.agent_error(
                str(error),
                details={"exception_type": type(error).__name__},
            )
            return

        yield AgentEvent.agent_start(prompt)

        try:
            async for event in self._agentic_loop():
                yield event
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - report unexpected agent failures
            yield AgentEvent.agent_error(
                str(error),
                details={"exception_type": type(error).__name__},
            )
        finally:
            self.state.abort_turn()

    async def _agentic_loop(self) -> AsyncGenerator[AgentEvent, None]:
        total_usage: TokenUsage | None = None

        for _ in range(self.max_iterations):
            request_messages = self.context_builder.build(self.state)

            response_parts: list[str] = []
            usage: TokenUsage | None = None
            finish_reason: str | None = None
            tool_calls: tuple[ToolCall, ...] = ()
            completed = False

            async for event in self.llm_client.chat_completion(
                request_messages,
                stream=True,
                tools=self.tool_registry.definitions() or None,
            ):
                if event.type == EventType.TEXT_DELTA and event.text_delta is not None:
                    response_parts.append(event.text_delta.content)
                    yield AgentEvent.text_delta(event.text_delta.content)
                elif event.type == EventType.MESSAGE_COMPLETE:
                    if event.text_delta is not None:
                        response_parts.append(event.text_delta.content)
                        yield AgentEvent.text_delta(event.text_delta.content)
                    usage = event.usage
                    finish_reason = event.finish_reason
                    tool_calls = event.tool_calls
                    completed = True
                elif event.type == EventType.ERROR:
                    yield AgentEvent.agent_error(
                        event.error or "The request failed without an error message."
                    )
                    return

            if not completed:
                yield AgentEvent.agent_error(
                    "The response stream ended before completion."
                )
                return

            if usage is not None:
                total_usage = usage if total_usage is None else total_usage + usage

            response = "".join(response_parts)

            if tool_calls:
                self.state.record_tool_calls(tool_calls, content=response or None)
                async for event in self._execute_tool_calls(tool_calls):
                    yield event
                continue

            if not response:
                yield AgentEvent.agent_error(
                    "The model completed without returning any text."
                )
                return

            self.state.complete_turn(response)
            yield AgentEvent.text_complete(response)
            yield AgentEvent.agent_end(response, total_usage, finish_reason)
            return

        yield AgentEvent.agent_error(
            "The agent stopped after reaching its limit of "
            f"{self.max_iterations} model requests without a final answer."
        )

    async def _execute_tool_calls(
        self,
        tool_calls: Sequence[ToolCall],
    ) -> AsyncGenerator[AgentEvent, None]:
        for call in tool_calls:
            yield AgentEvent.tool_call(call.call_id, call.name, call.arguments_json)

            rejected = False
            prepared = self.tool_executor.prepare(call)
            if isinstance(prepared, ToolResult):
                result = prepared
            else:
                if prepared.tool.approval is ToolApproval.REQUIRED:
                    request = ApprovalRequest.create(
                        call_id=call.call_id,
                        tool_name=call.name,
                        arguments=prepared.arguments.model_dump(mode="json"),
                        identity=self._approval_identity(),
                    )
                    response = self.approval_service.reusable_response(request)
                    if response is None:
                        yield AgentEvent.approval_requested(request)
                        response = await self.approval_service.authorize(request)
                    rejected = response.decision is ApprovalDecision.REJECTED

                if rejected:
                    result = ToolResult.failure(
                        call.call_id,
                        call.name,
                        "The user rejected this tool call.",
                        "approval_rejected",
                    )
                else:
                    invocation = self._tool_invocation(call)
                    task = asyncio.create_task(
                        self.tool_executor.execute_prepared(
                            prepared,
                            approved=True,
                            invocation=invocation,
                        )
                    )
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        invocation.cancellation_source.cancel("agent_cancelled")
                        await asyncio.gather(task, return_exceptions=True)
                        raise

            content = serialize_tool_result(result)
            self.state.record_tool_result(call.call_id, content)

            if rejected:
                yield AgentEvent.tool_rejected(call.call_id, call.name, content)
            else:
                yield AgentEvent.tool_result(
                    call.call_id,
                    result.tool_name,
                    result.status.value,
                    content,
                )

    def _approval_identity(self) -> ApprovalIdentity:
        identity = self._approval_identity_provider()
        if not isinstance(identity, ApprovalIdentity):
            raise TypeError(
                "approval identity provider must return an ApprovalIdentity."
            )
        return identity

    def _tool_invocation(self, call: ToolCall) -> ToolInvocationContext:
        turn_id = self.state.pending_turn_id
        if turn_id is None:
            raise RuntimeError("A tool call requires an active turn identity.")
        identity = self._approval_identity()
        execution = self._execution_context_factory.create(
            tool_call_id=call.call_id,
            session_id=identity.session_id,
            turn_id=turn_id,
            project_root=self._project_root,
        )
        if execution.workspace_id != identity.workspace_id:
            raise RuntimeError(
                "The approval identity does not match the active project workspace."
            )
        return ToolInvocationContext(
            execution=execution,
            cancellation_source=CancellationSource(),
        )

    async def _invoke_approval_handler(
        self,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        response = await self._approval_handler(request)
        if isinstance(response, ApprovalResponse):
            return response
        if response is ApprovalDecision.APPROVED:
            return ApprovalResponse.approve()
        if response is ApprovalDecision.REJECTED:
            return ApprovalResponse.reject()
        return response  # type: ignore[return-value]

    def reset(self) -> None:
        self.state.reset()

    async def close(self) -> None:
        await self.llm_client.close()


def run() -> None:
    """Launch the TrueCoder terminal application."""
    from truecoder.tui.app import TrueCoderApp

    launch_directory = Path.cwd().resolve(strict=True)
    project_root = find_project_root(launch_directory)
    project_instructions = load_project_instructions(
        project_root=project_root,
        launch_directory=launch_directory,
    )
    context_builder = ContextBuilder.from_environment(
        project_instructions=project_instructions,
    )
    state = AgentState()

    tool_registry = ToolRegistry()
    tool_registry.register(EditFileTool(project_root))
    tool_registry.register(GlobTool(project_root))
    tool_registry.register(GrepTool(project_root))
    tool_registry.register(ListDirTool(project_root))
    tool_registry.register(ReadFileTool(project_root))
    tool_registry.register(WriteFileTool(project_root))
    agent = Agent(
        state=state,
        context_builder=context_builder,
        tool_registry=tool_registry,
        project_root=project_root,
        execution_bootstrap_config=ExecutionBootstrapConfig(),
    )
    session_store = SQLiteSessionStore(default_session_database_path())
    session_manager = SessionManager(session_store, state, project_root)

    TrueCoderApp(agent, session_manager=session_manager).run()
