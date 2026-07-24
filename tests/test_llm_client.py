import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai import OpenAIError
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion import Choice as CompletionChoice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType


class FakeStream:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk

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
) -> ChatCompletionChunk:
    choices = []
    if content is not None or finish_reason is not None:
        choices.append(
            ChunkChoice(
                delta=ChoiceDelta(content=content),
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


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_non_streaming_emits_one_complete_event(self):
        response = ChatCompletion(
            id="completion-id",
            choices=[
                CompletionChoice(
                    finish_reason="stop",
                    index=0,
                    logprobs=None,
                    message=ChatCompletionMessage(
                        content="Complete answer",
                        refusal=None,
                        role="assistant",
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
        sdk_client, create = make_client(response)
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

    async def test_sdk_errors_are_returned_as_error_events(self):
        sdk_client, _ = make_client(None)
        sdk_client.chat.completions.create.side_effect = OpenAIError("request failed")
        llm_client = LLMClient()

        with (
            patch.dict(os.environ, {"MODEL": "test-model"}),
            patch.object(llm_client, "get_client", return_value=sdk_client),
        ):
            events = [
                event
                async for event in llm_client.chat_completion([], stream=False)
            ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.ERROR)
        self.assertIn("request failed", events[0].error)

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
