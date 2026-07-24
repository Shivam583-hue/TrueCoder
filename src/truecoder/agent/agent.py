from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from truecoder.agent.events import AgentEvent
from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType, TokenUsage


class Agent:
    """Manage conversation state and turn LLM responses into agent events."""

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client or LLMClient()
        self.messages: list[dict[str, Any]] = []

    async def run(self, prompt: str) -> AsyncGenerator[AgentEvent, None]:
        """Run one conversational turn and stream high-level events."""
        prompt = prompt.strip()
        if not prompt:
            yield AgentEvent.agent_error("The prompt cannot be empty.")
            return

        self.messages.append({"role": "user", "content": prompt})
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

    async def _agentic_loop(self) -> AsyncGenerator[AgentEvent, None]:
        """Execute the current agent turn.

        This is a single model call for now. Keeping it as a separate loop leaves
        a clear place to add tool calls and additional model steps later.
        """
        response_parts: list[str] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        request_messages = [message.copy() for message in self.messages]

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
            yield AgentEvent.agent_error(
                "The response stream ended before completion."
            )
            return

        response = "".join(response_parts)
        if not response:
            yield AgentEvent.agent_error(
                "The model completed without returning any text."
            )
            return

        self.messages.append({"role": "assistant", "content": response})
        yield AgentEvent.text_complete(response)
        yield AgentEvent.agent_end(response, usage, finish_reason)

    def reset(self) -> None:
        """Clear all conversation history."""
        self.messages.clear()

    async def close(self) -> None:
        """Release resources held by the underlying LLM client."""
        await self.llm_client.close()


def run() -> None:
    """Launch the TrueCoder terminal application."""
    from truecoder.tui.app import TrueCoderApp

    TrueCoderApp().run()
