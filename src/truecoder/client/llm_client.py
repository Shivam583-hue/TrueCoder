import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, cast

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AsyncStream,
    RateLimitError,
)
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage

from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
)
from truecoder.tools.base import ToolCall

load_dotenv()


@dataclass(slots=True)
class _ToolCallBuffer:
    call_id: str | None = None
    name: str | None = None
    argument_fragments: list[str] = field(default_factory=list)


class LLMClient:
    def __init__(self) -> None:
        self.__client: AsyncOpenAI | None = None
        self._max_retries: int = 3

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def get_client(self) -> AsyncOpenAI:
        if self.__client is None:
            api_key = os.getenv("API_KEY")
            if not api_key:
                raise RuntimeError("API_KEY must be set in the .env file")

            client_options: dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 0,
            }
            base_url = os.getenv("BASE_URL")
            if base_url:
                client_options["base_url"] = base_url

            self.__client = AsyncOpenAI(**client_options)

        return self.__client

    async def close(self) -> None:
        if self.__client is not None:
            await self.__client.close()
            self.__client = None

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        stream: bool = True,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        model = os.getenv("MODEL")
        if not model:
            raise RuntimeError("MODEL must be set in the .env file")

        client = self.get_client()

        if tools:
            request = {
                "model": model,
                "tools": tools,
                "messages": messages,
            }
        else:
            request = {
                "model": model,
                "messages": messages,
            }

        for attempt in range(self._max_retries + 1):
            stream_started = False

            try:
                if stream:
                    async for event in self._stream_response(client, request):
                        stream_started = True
                        yield event
                else:
                    yield await self._non_stream_response(client, request)
                return
            except RateLimitError as error:
                if stream_started:
                    yield StreamEvent(
                        type=EventType.ERROR,
                        error=f"Rate limit error while streaming: {error}",
                    )
                    return

                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue

                yield StreamEvent(
                    type=EventType.ERROR,
                    error=f"Rate limit exceeded: {error}",
                )
                return
            except APITimeoutError as error:
                if stream_started:
                    yield StreamEvent(
                        type=EventType.ERROR,
                        error=f"Request timed out while streaming: {error}",
                    )
                    return

                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue

                yield StreamEvent(
                    type=EventType.ERROR,
                    error=f"Request timed out: {error}",
                )
                return
            except APIConnectionError as error:
                if stream_started:
                    yield StreamEvent(
                        type=EventType.ERROR,
                        error=f"Connection error while streaming: {error}",
                    )
                    return

                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue

                yield StreamEvent(
                    type=EventType.ERROR,
                    error=f"Connection error: {error}",
                )
                return
            except APIError as error:
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=f"API error: {error}",
                )
                return

        async def _stream_response(
            self,
            client: AsyncOpenAI,
            request: dict[str, Any],
        ) -> AsyncGenerator[StreamEvent, None]:
            response = cast(
                AsyncStream[ChatCompletionChunk],
                await client.chat.completions.create(
                    **request,
                    stream=True,
                    stream_options={"include_usage": True},
                ),
            )

            usage: TokenUsage | None = None
            finish_reason: str | None = None
            tool_call_buffers: dict[int, _ToolCallBuffer] = {}

            async with response:
                async for chunk in response:
                    if chunk.usage is not None:
                        usage = self._to_token_usage(chunk.usage)

                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]

                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason

                    if choice.delta.content:
                        yield StreamEvent(
                            type=EventType.TEXT_DELTA,
                            text_delta=TextDelta(
                                content=choice.delta.content,
                            ),
                        )

                    for fragment in choice.delta.tool_calls or []:
                        index = fragment.index

                        if not isinstance(index, int) or index < 0:
                            yield StreamEvent(
                                type=EventType.ERROR,
                                error=(
                                    "The provider returned a tool-call fragment "
                                    "without a valid index."
                                ),
                            )
                            return

                        buffer = tool_call_buffers.setdefault(
                            index,
                            _ToolCallBuffer(),
                        )

                        if fragment.id is not None:
                            if (
                                buffer.call_id is not None
                                and buffer.call_id != fragment.id
                            ):
                                yield StreamEvent(
                                    type=EventType.ERROR,
                                    error=(
                                        f"Tool call at index {index} returned "
                                        "conflicting call IDs."
                                    ),
                                )
                                return

                            buffer.call_id = fragment.id

                        function = fragment.function
                        name = function.name if function is not None else None
                        arguments_fragment = (
                            function.arguments if function is not None else None
                        )

                        if name is not None:
                            if buffer.name is not None and buffer.name != name:
                                yield StreamEvent(
                                    type=EventType.ERROR,
                                    error=(
                                        f"Tool call at index {index} returned "
                                        "conflicting function names."
                                    ),
                                )
                                return

                            buffer.name = name

                        if arguments_fragment is not None:
                            buffer.argument_fragments.append(arguments_fragment)

                        yield StreamEvent(
                            type=EventType.TOOL_CALL_DELTA,
                            tool_call_delta=ToolCallDelta(
                                index=index,
                                call_id=fragment.id,
                                name=name,
                                arguments_fragment=arguments_fragment,
                            ),
                        )

            completed_tool_calls: list[ToolCall] = []

            for index in sorted(tool_call_buffers):
                buffer = tool_call_buffers[index]

                if not buffer.call_id:
                    yield StreamEvent(
                        type=EventType.ERROR,
                        error=(
                            f"Tool call at index {index} completed without a call ID."
                        ),
                    )
                    return

                if not buffer.name:
                    yield StreamEvent(
                        type=EventType.ERROR,
                        error=(
                            f"Tool call at index {index} completed without a function name."
                        ),
                    )
                    return

                arguments_json = "".join(buffer.argument_fragments)

                completed_tool_calls.append(
                    ToolCall(
                        call_id=buffer.call_id,
                        name=buffer.name,
                        arguments_json=arguments_json,
                    )
                )

            yield StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                finish_reason=finish_reason,
                usage=usage,
                tool_calls=tuple(completed_tool_calls),
            )

        async def _non_stream_response(
            self,
            client: AsyncOpenAI,
            request: dict[str, Any],
        ) -> StreamEvent:
            response = cast(
                ChatCompletion,
                await client.chat.completions.create(
                    **request,
                    stream=False,
                ),
            )

            usage = self._to_token_usage(response.usage)

            if not response.choices:
                return StreamEvent(
                    type=EventType.ERROR,
                    error="The model returned a response without any choices.",
                    usage=usage,
                )

            choice = response.choices[0]

            text_delta = (
                TextDelta(content=choice.message.content)
                if choice.message.content
                else None
            )

            tool_calls: list[ToolCall] = []

            for sdk_call in choice.message.tool_calls or []:
                try:
                    tool_call = ToolCall(
                        call_id=sdk_call.id,
                        name=sdk_call.function.name,
                        arguments_json=sdk_call.function.arguments,
                    )
                except (TypeError, ValueError) as error:
                    return StreamEvent(
                        type=EventType.ERROR,
                        error=f"The provider returned an invalid tool call: {error}",
                        usage=usage,
                    )

                tool_calls.append(tool_call)

            return StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                text_delta=text_delta,
                tool_calls=tuple(tool_calls),
                finish_reason=choice.finish_reason,
                usage=usage,
            )

    @staticmethod
    def _to_token_usage(usage: CompletionUsage | None) -> TokenUsage | None:
        if usage is None:
            return None

        prompt_details = usage.prompt_tokens_details
        cached_tokens = (
            prompt_details.cached_tokens
            if prompt_details is not None and prompt_details.cached_tokens is not None
            else 0
        )

        return TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=cached_tokens,
        )
