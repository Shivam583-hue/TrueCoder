from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from truecoder.client.llm_client import LLMClient
from truecoder.client.response import EventType
from truecoder.client.responses import responses_input, responses_tools
from truecoder.providers.models import ApiKey, SessionSettings
from truecoder.providers.oauth import OAuthToken
from truecoder.providers.openai import (
    OPENAI_CODEX_BASE_URL,
    OPENAI_CODEX_PROTOCOL_VERSION,
    OPENAI_CODEX_USER_AGENT,
    openai_provider,
)
from truecoder.tools.base import ToolCall


class FakeResponseStream:
    def __init__(self, events) -> None:
        self.events = events
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


def sdk_client(response):
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client, create


def settings() -> SessionSettings:
    return SessionSettings(
        provider=openai_provider(),
        credential=ApiKey("sk-openai"),
        model="gpt-5.2",
    )


def usage():
    return SimpleNamespace(
        input_tokens=12,
        output_tokens=5,
        total_tokens=17,
        input_tokens_details=SimpleNamespace(cached_tokens=7),
    )


class ConversionTests(unittest.TestCase):
    def test_chat_history_becomes_responses_items(self):
        converted = responses_input(
            [
                {"role": "system", "content": "Be exact."},
                {"role": "user", "content": "Read it."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"a"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "body"},
            ]
        )

        self.assertEqual(converted[0], {"role": "system", "content": "Be exact."})
        self.assertEqual(converted[2]["type"], "function_call")
        self.assertEqual(converted[2]["call_id"], "call-1")
        self.assertEqual(converted[3]["type"], "function_call_output")
        self.assertEqual(converted[3]["output"], "body")

    def test_chat_tools_become_flat_response_tools(self):
        converted = responses_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read a file.",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                }
            ]
        )

        self.assertEqual(
            converted,
            [
                {
                    "type": "function",
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                    "strict": True,
                }
            ],
        )


class ResponsesClientTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, llm_client, client, *, stream, messages=(), tools=None):
        with patch.object(llm_client, "get_client", return_value=client):
            return [
                event
                async for event in llm_client.chat_completion(
                    messages,
                    stream=stream,
                    tools=tools,
                )
            ]

    async def test_streaming_text_and_tools_use_the_responses_endpoint(self):
        function = SimpleNamespace(
            type="function_call",
            call_id="call-1",
            name="read",
            arguments="",
        )
        done_function = SimpleNamespace(
            type="function_call",
            call_id="call-1",
            name="read",
            arguments='{"path":"a"}',
        )
        completed = SimpleNamespace(usage=usage())
        stream = FakeResponseStream(
            [
                SimpleNamespace(
                    type="response.output_text.delta",
                    delta="Checking",
                ),
                SimpleNamespace(
                    type="response.output_item.added",
                    output_index=1,
                    item=function,
                ),
                SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=1,
                    delta='{"path":',
                ),
                SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=1,
                    delta='"a"}',
                ),
                SimpleNamespace(
                    type="response.output_item.done",
                    output_index=1,
                    item=done_function,
                ),
                SimpleNamespace(type="response.completed", response=completed),
            ]
        )
        client, create = sdk_client(stream)
        definition = {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {"type": "object"},
            },
        }

        events = await self.collect(
            LLMClient(settings()),
            client,
            stream=True,
            messages=({"role": "user", "content": "Read a"},),
            tools=[definition],
        )

        self.assertEqual(
            [event.type for event in events],
            [
                EventType.TEXT_DELTA,
                EventType.TOOL_CALL_DELTA,
                EventType.TOOL_CALL_DELTA,
                EventType.TOOL_CALL_DELTA,
                EventType.MESSAGE_COMPLETE,
            ],
        )
        self.assertEqual(events[0].text_delta.content, "Checking")
        self.assertEqual(
            events[-1].tool_calls,
            (ToolCall("call-1", "read", '{"path":"a"}'),),
        )
        self.assertEqual(events[-1].finish_reason, "tool_calls")
        self.assertEqual(events[-1].usage.cached_tokens, 7)
        self.assertTrue(stream.closed)
        create.assert_awaited_once_with(
            model="gpt-5.2",
            input=[{"role": "user", "content": "Read a"}],
            store=False,
            tools=[
                {
                    "type": "function",
                    "name": "read",
                    "parameters": {"type": "object"},
                    "strict": False,
                }
            ],
            stream=True,
        )

    async def test_non_streaming_returns_text_tools_and_usage(self):
        function = SimpleNamespace(
            type="function_call",
            call_id="call-1",
            name="read",
            arguments='{"path":"a"}',
        )
        response = SimpleNamespace(
            status="completed",
            output_text="Done",
            output=[function],
            usage=usage(),
        )
        client, create = sdk_client(response)

        events = await self.collect(
            LLMClient(settings()),
            client,
            stream=False,
            messages=({"role": "user", "content": "Read a"},),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.MESSAGE_COMPLETE)
        self.assertEqual(events[0].text_delta.content, "Done")
        self.assertEqual(
            events[0].tool_calls,
            (ToolCall("call-1", "read", '{"path":"a"}'),),
        )
        self.assertEqual(events[0].usage.total_tokens, 17)
        create.assert_awaited_once_with(
            model="gpt-5.2",
            input=[{"role": "user", "content": "Read a"}],
            store=False,
            stream=False,
        )

    async def test_a_failed_response_becomes_an_error_event(self):
        response = SimpleNamespace(
            status="failed",
            error=SimpleNamespace(message="subscription refused"),
            usage=None,
        )
        client, _ = sdk_client(response)

        events = await self.collect(
            LLMClient(settings()),
            client,
            stream=False,
        )

        self.assertEqual(events[0].type, EventType.ERROR)
        self.assertEqual(events[0].error, "subscription refused")

    async def test_an_oauth_client_uses_the_codex_endpoint_and_account(self):
        token = OAuthToken(
            access_token="at-openai",
            provider="openai",
            metadata=(("ChatGPT-Account-Id", "acct-1"),),
            endpoint=OPENAI_CODEX_BASE_URL,
        )
        active = settings()
        active.credential = token
        llm_client = LLMClient(active)

        client = llm_client.get_client()

        self.assertEqual(str(client.base_url).rstrip("/"), OPENAI_CODEX_BASE_URL)
        self.assertEqual(client.default_headers["ChatGPT-Account-Id"], "acct-1")
        self.assertEqual(client.default_headers["originator"], "truecoder")
        self.assertEqual(
            client.default_headers["version"],
            OPENAI_CODEX_PROTOCOL_VERSION,
        )
        self.assertEqual(
            client.default_headers["User-Agent"],
            OPENAI_CODEX_USER_AGENT,
        )
        await llm_client.close()


if __name__ == "__main__":
    unittest.main()
