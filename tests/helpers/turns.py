from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tools import ToolCall


class TokenCounter:
    def count_message(self, message: Any) -> int:
        return len(json.dumps(message, default=str)) // 4


class ScriptedModel:
    def __init__(self, batches: Sequence[Sequence[StreamEvent]]) -> None:
        self.batches = [list(batch) for batch in batches]
        self.requests: list[list[Any]] = []

    async def chat_completion(self, messages, stream=True, tools=None):
        del stream, tools
        index = len(self.requests)
        self.requests.append(list(messages))
        batch = self.batches[index] if index < len(self.batches) else []
        for event in batch:
            yield event

    async def close(self) -> None:
        return None

    def tool_results(self) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        for request in self.requests:
            for message in request:
                if message.get("role") != "tool":
                    continue
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                try:
                    seen.append(json.loads(content))
                except json.JSONDecodeError:
                    seen.append({"raw": content})
        return seen

    def last_result_for(self, key: str, value: str) -> dict[str, Any] | None:
        for payload in reversed(self.tool_results()):
            for candidate in (payload.get("output"), payload):
                if isinstance(candidate, dict) and candidate.get(key) == value:
                    return candidate
        return None

    def envelope_for(self, key: str, value: str) -> dict[str, Any] | None:
        for payload in reversed(self.tool_results()):
            output = payload.get("output")
            if isinstance(output, dict) and output.get(key) == value:
                return payload
            if payload.get(key) == value:
                return payload
        return None


def calls(*requests: tuple[str, dict[str, Any]]) -> list[StreamEvent]:
    return [
        StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            tool_calls=tuple(
                ToolCall(f"call_{index}", name, json.dumps(arguments))
                for index, (name, arguments) in enumerate(requests, start=1)
            ),
            finish_reason="tool_calls",
        )
    ]


def says(text: str) -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
    ]
