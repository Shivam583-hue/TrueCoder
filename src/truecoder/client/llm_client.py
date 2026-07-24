import os
from re import finditer
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from openai import AsyncOpenAI

from truecoder.client.response import EventType, StreamEvent, TextDelta, TokenUsage

load_dotenv()


class LLMClient:
    def __init__(self) -> None:
        self.__client: AsyncOpenAI | None = None

    def get_client(self) -> AsyncOpenAI:
        if self.__client is None:
            api_key = os.getenv("API_KEY")
            base_url = os.getenv("BASE_URL")
            if not api_key or not base_url:
                raise RuntimeError("API_KEY and BASE_URL must be set in the .env file")
            self.__client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self.__client

    async def close(self) -> None:
        if self.__client:
            await self.__client.close()
            self.__client = None

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        client = self.get_client()
        keywargs = {
            "model": os.getenv("MODEL"),
            "messages": messages,
            "stream": stream,
        }
        if stream:
            async for event in self._stream_response(client, keywargs):
                yield event
        else:
            event = await self._non_stream_response(client, keywargs)
            yield event
        return

    async def _stream_response(
        self, client: AsyncOpenAI, keywargs: dict[str, Any]
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**keywargs)

        usage: TokenUsage | None = None
        finish_reason: str | None = None

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                    cached_tokens=chunk.usage.prompt_tokens_details.cached_tokens,
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta.content:
                yield StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta(delta.content),
                )

        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _non_stream_response(
        self, client: AsyncOpenAI, keywargs: dict[str, Any]
    ) -> StreamEvent:
        response = await client.chat.completions.create(**keywargs)
        choice = response.choices[0]
        message = choice.message

        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)

        usage = None
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cached_tokens=response.usage.prompt_tokens_details.cached_tokens,
            )
        return StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
