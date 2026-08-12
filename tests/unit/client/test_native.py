from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from truecoder.client.llm_client import LLMClient
from truecoder.client.native import (
    _anthropic_stream,
    _google_stream,
    anthropic_request,
    google_request,
)
from truecoder.client.response import EventType, StreamEvent
from truecoder.providers import ApiKey, Provider, SessionSettings

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]

MESSAGES = [
    {"role": "system", "content": "Be precise."},
    {"role": "user", "content": "Read it."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call-1", "content": "hello"},
]


class _Lines:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    async def aiter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)


class RequestTranslationTests(unittest.TestCase):
    def test_anthropic_receives_native_messages_and_tools(self):
        request = anthropic_request("claude", MESSAGES, TOOLS, stream=True)

        self.assertEqual(request["system"], "Be precise.")
        self.assertEqual(request["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(
            request["messages"][2]["content"][0]["tool_use_id"],
            "call-1",
        )
        self.assertEqual(request["tools"][0]["input_schema"]["type"], "object")

    def test_google_receives_native_contents_and_function_declarations(self):
        request = google_request(MESSAGES, TOOLS)

        self.assertEqual(
            request["systemInstruction"]["parts"][0]["text"],
            "Be precise.",
        )
        self.assertEqual(request["contents"][1]["role"], "model")
        response = request["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "read_file")
        declaration = request["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "read_file")


class StreamTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_streams_text_tools_and_usage(self):
        response = _Lines(
            [
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 5}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "read_file",
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"path":"README.md"}',
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 3},
                },
            ]
        )

        events = [event async for event in _anthropic_stream(response)]

        self.assertEqual(events[0].text_delta.content, "Hello")
        self.assertEqual(events[-1].tool_calls[0].call_id, "call-1")
        self.assertEqual(events[-1].finish_reason, "tool_calls")
        self.assertEqual(events[-1].usage.total_tokens, 8)

    async def test_google_streams_text_tools_and_usage(self):
        response = _Lines(
            [
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "Hello"}]},
                        }
                    ]
                },
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "call-1",
                                            "name": "read_file",
                                            "args": {"path": "README.md"},
                                        }
                                    }
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 4,
                        "candidatesTokenCount": 2,
                    },
                },
            ]
        )

        events = [event async for event in _google_stream(response)]

        self.assertEqual(events[0].text_delta.content, "Hello")
        self.assertEqual(events[-1].tool_calls[0].name, "read_file")
        self.assertEqual(events[-1].finish_reason, "tool_calls")
        self.assertEqual(events[-1].usage.total_tokens, 6)


class NativeDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_client_bypasses_openai_for_anthropic(self):
        settings = SessionSettings(
            provider=Provider(
                name="anthropic",
                base_url="https://api.anthropic.invalid/v1",
                adapter="anthropic",
            ),
            credential=ApiKey("sk-ant"),
            model="claude",
        )
        client = LLMClient(settings)

        async def completion(*args, **kwargs):
            yield StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop")

        with (
            patch(
                "truecoder.client.llm_client.native_completion",
                side_effect=completion,
            ) as native,
            patch.object(client, "get_client") as openai,
        ):
            events = [
                event
                async for event in client.chat_completion(
                    [{"role": "user", "content": "hello"}],
                    stream=True,
                )
            ]

        self.assertEqual(events[-1].type, EventType.MESSAGE_COMPLETE)
        native.assert_called_once()
        openai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
