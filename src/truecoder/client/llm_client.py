import os
from typing import Any, AsyncGenerator, cast

from dotenv import load_dotenv
from openai import AsyncOpenAI, AsyncStream, OpenAIError
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage

from truecoder.client.response import EventType, StreamEvent, TextDelta, TokenUsage

load_dotenv()


class LLMClient:
    def __init__(self) -> None:
        self.__client: AsyncOpenAI | None = None

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def get_client(self) -> AsyncOpenAI:
        if self.__client is None:
            api_key = os.getenv("API_KEY")
            if not api_key:
                raise RuntimeError("API_KEY must be set in the .env file")

            client_options: dict[str, Any] = {"api_key": api_key}
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
    ) -> AsyncGenerator[StreamEvent, None]:
        model = os.getenv("MODEL")
        if not model:
            raise RuntimeError("MODEL must be set in the .env file")

        client = self.get_client()
        request = {
            "model": model,
            "messages": messages,
        }

        try:
            if stream:
                async for event in self._stream_response(client, request):
                    yield event
            else:
                yield await self._non_stream_response(client, request)
        except OpenAIError as error:
            yield StreamEvent(type=EventType.ERROR, error=str(error))

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
                        text_delta=TextDelta(content=choice.delta.content),
                    )

        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
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

        if not response.choices:
            return StreamEvent(
                type=EventType.ERROR,
                error="The model returned a response without any choices.",
                usage=self._to_token_usage(response.usage),
            )

        choice = response.choices[0]
        text_delta = (
            TextDelta(content=choice.message.content)
            if choice.message.content
            else None
        )

        return StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=self._to_token_usage(response.usage),
        )

    @staticmethod
    def _to_token_usage(usage: CompletionUsage | None) -> TokenUsage | None:
        if usage is None:
            return None

        prompt_details = usage.prompt_tokens_details
        cached_tokens = (
            prompt_details.cached_tokens
            if prompt_details is not None
            and prompt_details.cached_tokens is not None
            else 0
        )

        return TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=cached_tokens,
        )
