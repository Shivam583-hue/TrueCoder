from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from truecoder.agent.approval import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
    reject_all_tool_calls,
)
from truecoder.agent.context import ContextBuilder
from truecoder.agent.events import AgentEvent
from truecoder.agent.messages import ModelMessage
from truecoder.agent.state import AgentState
from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage
from truecoder.tools import ToolExecutor, serialize_tool_result
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArgumentError,
    ToolCall,
    ToolResult,
)
from truecoder.tools.builtin import ReadFileTool
from truecoder.tools.registry import ToolNotFoundError, ToolRegistry

DEFAULT_MAX_ITERATIONS = 25


class Agent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        state: AgentState | None = None,
        context_builder: ContextBuilder | None = None,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise TypeError("max_iterations must be an integer.")

        if max_iterations < 1:
            raise ValueError("max_iterations must be at least one.")

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
        self.approval_handler: ApprovalHandler = (
            approval_handler if approval_handler is not None else reject_all_tool_calls
        )

    @property
    def messages(self) -> list[ModelMessage]:
        return self.state.messages

    async def run(self, prompt: str) -> AsyncGenerator[AgentEvent, None]:
        prompt = prompt.strip()
        if not prompt:
            yield AgentEvent.agent_error("The prompt cannot be empty.")
            return
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
            tool = self._lookup_tool(call.name)
            if tool is not None and tool.approval is ToolApproval.REQUIRED:
                arguments = self._display_arguments(tool, call)
                if arguments is not None:
                    request = ApprovalRequest(call.call_id, call.name, arguments)
                    yield AgentEvent.approval_requested(
                        request.call_id,
                        request.tool_name,
                        request.arguments,
                    )
                    decision = await self.approval_handler(request)
                    rejected = decision is ApprovalDecision.REJECTED

            if rejected:
                result = ToolResult.failure(
                    call.call_id,
                    call.name,
                    "The user rejected this tool call.",
                    "approval_rejected",
                )
            else:
                result = await self.tool_executor.execute(call, approved=True)

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

    def _lookup_tool(self, name: str) -> BaseTool[Any] | None:
        try:
            return self.tool_registry.get(name)
        except ToolNotFoundError:
            return None

    @staticmethod
    def _display_arguments(
        tool: BaseTool[Any],
        call: ToolCall,
    ) -> dict[str, Any] | None:
        try:
            return tool.parse_arguments(call.arguments_json).model_dump()
        except ToolArgumentError:
            return None

    def reset(self) -> None:
        self.state.reset()

    async def close(self) -> None:
        await self.llm_client.close()


def run() -> None:
    """Launch the TrueCoder terminal application."""
    from truecoder.tui.app import TrueCoderApp

    workspace_root = Path.cwd().resolve(strict=True)
    tool_registry = ToolRegistry()
    tool_registry.register(ReadFileTool(workspace_root))
    agent = Agent(tool_registry=tool_registry)

    TrueCoderApp(agent).run()
