from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from truecoder.agent.context import ContextBuilder
from truecoder.agent.events import AgentEvent
from truecoder.agent.state import AgentState
from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage


class Agent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        state: AgentState | None = None,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.llm_client = llm_client if llm_client is not None else LLMClient()
        self.state = state if state is not None else AgentState()
        self.context_builder = (
            context_builder
            if context_builder is not None
            else ContextBuilder.from_environment()
        )

    @property
    def messages(self) -> list[dict[str, Any]]:
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
        response_parts: list[str] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        request_messages = self.context_builder.build(self.state)

        async for event in self.llm_client.chat_completion(
            request_messages,
            stream=True,
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
                completed = True
            elif event.type == EventType.ERROR:
                yield AgentEvent.agent_error(
                    event.error or "The request failed without an error message."
                )
                return

        if not completed:
            yield AgentEvent.agent_error("The response stream ended before completion.")
            return

        response = "".join(response_parts)
        if not response:
            yield AgentEvent.agent_error(
                "The model completed without returning any text."
            )
            return

        self.state.complete_turn(response)
        yield AgentEvent.text_complete(response)
        yield AgentEvent.agent_end(response, usage, finish_reason)

    def reset(self) -> None:
        self.state.reset()

    async def close(self) -> None:
        await self.llm_client.close()


def run() -> None:
    """Launch the TrueCoder terminal application."""
    from truecoder.tui.app import TrueCoderApp

    TrueCoderApp().run()
