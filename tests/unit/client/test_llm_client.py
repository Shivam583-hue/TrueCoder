import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    RateLimitError,
)
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion import Choice as CompletionChoice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import (
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType
from truecoder.tools import ToolCall


class FakeStream:
    def __init__(
        self,
        chunks: list[ChatCompletionChunk],
        error: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


def make_client(response: object):
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )
    return client, create


def make_chunk(
    content: str | None = None,
    finish_reason: str | None = None,
    usage: CompletionUsage | None = None,
    tool_calls: list[ChoiceDeltaToolCall] | None = None,
) -> ChatCompletionChunk:
    choices = []
    if content is not None or finish_reason is not None or tool_calls is not None:
        choices.append(
            ChunkChoice(
                delta=ChoiceDelta(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
                index=0,
                logprobs=None,
            )
        )

    return ChatCompletionChunk(
        id="chunk-id",
        choices=choices,
        created=0,
        model="test-model",
        object="chat.completion.chunk",
        usage=usage,
    )


def make_tool_delta(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> ChoiceDeltaToolCall:
    function = (
        ChoiceDeltaToolCallFunction(
            name=name,
            arguments=arguments,
        )
        if name is not None or arguments is not None
        else None
    )
    return ChoiceDeltaToolCall(
        index=index,
        id=call_id,
        type="function" if call_id is not None else None,
        function=function,
    )


def make_completion(
    content: str | None = "Complete answer",
    *,
    tool_calls: list[ChatCompletionMessageFunctionToolCall] | None = None,
    finish_reason: str = "stop",
) -> ChatCompletion:
    return ChatCompletion(
        id="completion-id",
        choices=[
            CompletionChoice(
                finish_reason=finish_reason,
                index=0,
                logprobs=None,
                message=ChatCompletionMessage(
                    content=content,
                    refusal=None,
                    role="assistant",
                    tool_calls=tool_calls,
                ),
            )
        ],
        created=0,
        model="test-model",
        object="chat.completion",
        usage=CompletionUsage(
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
            prompt_tokens_details=None,
        ),
    )


def make_rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.example.com/chat/completions")
    response = httpx.Response(429, request=request)
    return RateLimitError(
        "too many requests",
        response=response,
        body={"code": "rate_limit_exceeded"},
    )


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def _collect_events(
        self,
        llm_client: LLMClient,
        sdk_client,
        *,
        stream: bool,
        tools: list[dict] | None = None,
    ):
        with (
            patch.dict(os.environ, {"MODEL": "test-model"}),
            patch.object(llm_client, "get_client", return_value=sdk_client),
        ):
            return [
                event
                async for event in llm_client.chat_completion(
                    [],
                    stream=stream,
                    tools=tools,
                )
            ]

    async def test_streaming_emits_deltas_then_completion_with_usage(self):
        usage = CompletionUsage(
            prompt_tokens=4,
            completion_tokens=2,
            total_tokens=6,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=3),
        )
        stream = FakeStream(
            [
                make_chunk(content="Hello"),
                make_chunk(content=" world"),
                make_chunk(finish_reason="stop"),
                make_chunk(usage=usage),
            ]
        )
        sdk_client, create = make_client(stream)
        llm_client = LLMClient()
        messages = [{"role": "user", "content": "Hi"}]

        with (
            patch.dict(os.environ, {"MODEL": "test-model"}),
            patch.object(llm_client, "get_client", return_value=sdk_client),
        ):
            events = [
                event
                async for event in llm_client.chat_completion(messages, stream=True)
            ]

        self.assertEqual(
            [event.type for event in events],
            [
                EventType.TEXT_DELTA,
                EventType.TEXT_DELTA,
                EventType.MESSAGE_COMPLETE,
            ],
        )
        self.assertEqual(events[0].text_delta.content, "Hello")
        self.assertEqual(events[1].text_delta.content, " world")
        self.assertEqual(events[2].finish_reason, "stop")
        self.assertEqual(events[2].usage.cached_tokens, 3)
        self.assertTrue(stream.closed)
        create.assert_awaited_once_with(
            model="test-model",
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
        )

    async def test_streaming_assembles_fragmented_tool_call(self):
        stream = FakeStream(
            [
                make_chunk(
                    tool_calls=[
                        make_tool_delta(
                            0,
                            call_id="call_1",
                            name="add",
                            arguments='{"left":',
                        )
                    ]
                ),
                make_chunk(
                    tool_calls=[
                        make_tool_delta(
                            0,
                            arguments='2,"right":3}',
                        )
                    ]
                ),
                make_chunk(finish_reason="tool_calls"),
            ]
        )
        sdk_client, _ = make_client(stream)

        events = await self._collect_events(
            LLMClient(),
            sdk_client,
            stream=True,
        )

        self.assertEqual(
            [event.type for event in events],
            [
                EventType.TOOL_CALL_DELTA,
                EventType.TOOL_CALL_DELTA,
                EventType.MESSAGE_COMPLETE,
            ],
        )
        self.assertEqual(events[0].tool_call_delta.index, 0)
        self.assertEqual(events[0].tool_call_delta.call_id, "call_1")
        self.assertEqual(events[0].tool_call_delta.name, "add")
        self.assertEqual(events[0].tool_call_delta.arguments_delta, '{"left":')
        self.assertEqual(
            events[1].tool_call_delta.arguments_delta,
            '2,"right":3}',
        )
        self.assertEqual(
            events[2].tool_calls,
            (
                ToolCall(
                    call_id="call_1",
                    name="add",
                    arguments_json='{"left":2,"right":3}',
                ),
            ),
        )
        self.assertEqual(events[2].finish_reason, "tool_calls")

    async def test_streaming_assembles_interleaved_calls_by_index(self):
        stream = FakeStream(
            [
                make_chunk(
                    tool_calls=[
                        make_tool_delta(
                            1,
                            call_id="call_2",
                            name="second",
                            arguments='{"value":',
                        ),
                        make_tool_delta(
                            0,
                            call_id="call_1",
                            name="first",
                            arguments='{"value":',
                        ),
                    ]
                ),
                make_chunk(
                    tool_calls=[
                        make_tool_delta(1, arguments="2}"),
                        make_tool_delta(0, arguments="1}"),
                    ]
                ),
            ]
        )
        sdk_client, _ = make_client(stream)

        events = await self._collect_events(
            LLMClient(),
            sdk_client,
            stream=True,
        )

        self.assertEqual(
            events[-1].tool_calls,
            (
                ToolCall("call_1", "first", '{"value":1}'),
                ToolCall("call_2", "second", '{"value":2}'),
            ),
        )

    async def test_streaming_rejects_an_incomplete_tool_call(self):
        stream = FakeStream(
            [
                make_chunk(
                    tool_calls=[
                        make_tool_delta(
                            0,
                            name="add",
                            arguments="{}",
                        )
                    ]
                )
            ]
        )
        sdk_client, _ = make_client(stream)

        events = await self._collect_events(
            LLMClient(),
            sdk_client,
            stream=True,
        )

        self.assertEqual(
            [event.type for event in events],
            [EventType.TOOL_CALL_DELTA, EventType.ERROR],
        )
        self.assertIn("without a call ID", events[-1].error)

    async def test_non_streaming_emits_one_complete_event(self):
        sdk_client, create = make_client(make_completion())
        llm_client = LLMClient()
        messages = [{"role": "user", "content": "Hi"}]

        with (
            patch.dict(os.environ, {"MODEL": "test-model"}),
            patch.object(llm_client, "get_client", return_value=sdk_client),
        ):
            events = [
                event
                async for event in llm_client.chat_completion(messages, stream=False)
            ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.MESSAGE_COMPLETE)
        self.assertEqual(events[0].text_delta.content, "Complete answer")
        self.assertEqual(events[0].usage.cached_tokens, 0)
        create.assert_awaited_once_with(
            model="test-model",
            messages=messages,
            stream=False,
        )

    async def test_non_streaming_exposes_tool_calls(self):
        completion = make_completion(
            None,
            tool_calls=[
                ChatCompletionMessageFunctionToolCall(
                    id="call_1",
                    type="function",
                    function=Function(
                        name="add",
                        arguments='{"left":2,"right":3}',
                    ),
                )
            ],
            finish_reason="tool_calls",
        )
        sdk_client, _ = make_client(completion)

        events = await self._collect_events(
            LLMClient(),
            sdk_client,
            stream=False,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.MESSAGE_COMPLETE)
        self.assertIsNone(events[0].text_delta)
        self.assertEqual(
            events[0].tool_calls,
            (
                ToolCall(
                    "call_1",
                    "add",
                    '{"left":2,"right":3}',
                ),
            ),
        )
        self.assertEqual(events[0].finish_reason, "tool_calls")

    async def test_tool_definitions_are_sent_only_when_non_empty(self):
        definition = {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two integers.",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }
        sdk_client, create = make_client(make_completion())

        await self._collect_events(
            LLMClient(),
            sdk_client,
            stream=False,
            tools=[definition],
        )

        create.assert_awaited_once_with(
            model="test-model",
            messages=[],
            tools=[definition],
            stream=False,
        )

        empty_sdk_client, empty_create = make_client(make_completion())
        await self._collect_events(
            LLMClient(),
            empty_sdk_client,
            stream=False,
            tools=[],
        )
        empty_create.assert_awaited_once_with(
            model="test-model",
            messages=[],
            stream=False,
        )

    async def test_rate_limit_retries_with_exponential_backoff(self):
        sdk_client, create = make_client(None)
        create.side_effect = [make_rate_limit_error() for _ in range(4)]
        llm_client = LLMClient()

        with patch(
            "truecoder.client.llm_client.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            events = await self._collect_events(
                llm_client,
                sdk_client,
                stream=False,
            )

        self.assertEqual(create.await_count, 4)
        sleep.assert_has_awaits([call(1), call(2), call(4)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.ERROR)
        self.assertIn("Rate limit exceeded", events[0].error)

    async def test_connection_error_retries_and_can_recover(self):
        request = httpx.Request(
            "POST",
            "https://api.example.com/chat/completions",
        )
        sdk_client, create = make_client(None)
        create.side_effect = [
            APIConnectionError(request=request),
            make_completion("Recovered"),
        ]
        llm_client = LLMClient()

        with patch(
            "truecoder.client.llm_client.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            events = await self._collect_events(
                llm_client,
                sdk_client,
                stream=False,
            )

        self.assertEqual(create.await_count, 2)
        sleep.assert_awaited_once_with(1)
        self.assertEqual(events[0].type, EventType.MESSAGE_COMPLETE)
        self.assertEqual(events[0].text_delta.content, "Recovered")

    async def test_timeout_has_a_specific_error_message(self):
        request = httpx.Request(
            "POST",
            "https://api.example.com/chat/completions",
        )
        sdk_client, create = make_client(None)
        create.side_effect = APITimeoutError(request=request)
        llm_client = LLMClient()
        llm_client._max_retries = 0

        events = await self._collect_events(
            llm_client,
            sdk_client,
            stream=False,
        )

        create.assert_awaited_once()
        self.assertEqual(events[0].type, EventType.ERROR)
        self.assertIn("Request timed out", events[0].error)

    async def test_api_error_is_returned_without_retrying(self):
        request = httpx.Request(
            "POST",
            "https://api.example.com/chat/completions",
        )
        sdk_client, create = make_client(None)
        create.side_effect = APIError("bad response", request, body=None)
        llm_client = LLMClient()

        with patch(
            "truecoder.client.llm_client.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            events = await self._collect_events(
                llm_client,
                sdk_client,
                stream=False,
            )

        create.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertEqual(events[0].type, EventType.ERROR)
        self.assertIn("API error", events[0].error)

    async def test_stream_error_after_a_delta_does_not_retry(self):
        stream = FakeStream(
            [make_chunk(content="Partial")],
            error=make_rate_limit_error(),
        )
        sdk_client, create = make_client(stream)
        llm_client = LLMClient()

        with patch(
            "truecoder.client.llm_client.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            events = await self._collect_events(
                llm_client,
                sdk_client,
                stream=True,
            )

        create.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertTrue(stream.closed)
        self.assertEqual(
            [event.type for event in events],
            [EventType.TEXT_DELTA, EventType.ERROR],
        )
        self.assertIn("while streaming", events[1].error)

    async def test_unexpected_errors_are_not_hidden(self):
        sdk_client, create = make_client(None)
        create.side_effect = ValueError("programming error")
        llm_client = LLMClient()

        with self.assertRaisesRegex(ValueError, "programming error"):
            await self._collect_events(
                llm_client,
                sdk_client,
                stream=False,
            )

    async def test_context_manager_closes_an_initialized_client(self):
        sdk_client = SimpleNamespace(close=AsyncMock())
        llm_client = LLMClient()
        llm_client._LLMClient__client = sdk_client

        async with llm_client:
            pass

        sdk_client.close.assert_awaited_once()
        self.assertIsNone(llm_client._LLMClient__client)

    async def test_missing_model_is_a_configuration_error(self):
        llm_client = LLMClient()

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MODEL"):
                _ = [
                    event
                    async for event in llm_client.chat_completion([], stream=False)
                ]


if __name__ == "__main__":
    unittest.main()
