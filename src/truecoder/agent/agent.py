from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from truecoder.agent.context import ContextBuilder
from truecoder.agent.events import AgentEvent
from truecoder.agent.messages import ModelMessage
from truecoder.agent.state import AgentState
from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage
from truecoder.tools import ToolExecutor, serialize_tool_result
from truecoder.tools.base import ToolCall
from truecoder.tools.builtin import ReadFileTool
from truecoder.tools.registry import ToolRegistry

DEFAULT_MAX_ITERATIONS = 25


class Agent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        state: AgentState | None = None,
        context_builder: ContextBuilder | None = None,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
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
        except Exception as error:
            yield AgentEvent.agent_error(
                str(error),
                details={"exception_type": type(error).__name__},
            )
        finally:
            self.state.abort_turn()

    async def _agentic_loop(self) -> AsyncGenerator[AgentEvent, None]:
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
            yield AgentEvent.agent_end(response, usage, finish_reason)
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

            # Interactive approval is a later phase; this phase auto-approves every
            # tool call while keeping the executor's approval gate intact.
            result = await self.tool_executor.execute(call, approved=True)
            content = serialize_tool_result(result)
            self.state.record_tool_result(call.call_id, content)

            yield AgentEvent.tool_result(
                call.call_id,
                result.tool_name,
                result.status.value,
                content,
            )

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
