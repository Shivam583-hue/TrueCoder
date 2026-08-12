from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
)
from truecoder.tools.base import ToolCall


@dataclass(slots=True)
class ResponseToolBuffer:
    call_id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


def responses_input(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content", ""),
                }
            )
            continue

        content = message.get("content")
        if isinstance(content, str) and (content or role != "assistant"):
            items.append({"role": role, "content": content})

        if role != "assistant":
            continue
        for call in message.get("tool_calls", ()):
            function = call.get("function", {})
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", ""),
                }
            )
    return items


def responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            continue
        item = {
            "type": "function",
            "name": function.get("name", ""),
            "parameters": function.get("parameters"),
            "strict": function.get("strict", False),
        }
        description = function.get("description")
        if isinstance(description, str):
            item["description"] = description
        converted.append(item)
    return converted


def responses_request(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "input": responses_input(messages),
        "store": False,
    }
    if tools:
        request["tools"] = responses_tools(tools)
    return request


def response_usage(usage: object | None) -> TokenUsage | None:
    if usage is None:
        return None
    input_details = getattr(usage, "input_tokens_details", None)
    return TokenUsage(
        prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cached_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
    )


def response_error(response: object) -> str:
    error = getattr(response, "error", None)
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        return message
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None)
    if isinstance(reason, str) and reason:
        return f"The model response was incomplete: {reason}."
    return "The model did not complete the response."


def completed_tool_calls(
    buffers: dict[int, ResponseToolBuffer],
) -> tuple[ToolCall, ...] | str:
    calls: list[ToolCall] = []
    for index in sorted(buffers):
        buffer = buffers[index]
        if not buffer.call_id:
            return f"Tool call at index {index} completed without a call ID."
        if not buffer.name:
            return f"Tool call at index {index} completed without a function name."
        calls.append(
            ToolCall(
                call_id=buffer.call_id,
                name=buffer.name,
                arguments_json="".join(buffer.arguments),
            )
        )
    return tuple(calls)


def complete_event(
    buffers: dict[int, ResponseToolBuffer],
    usage: TokenUsage | None,
    *,
    finish_reason: str | None = None,
) -> StreamEvent:
    calls = completed_tool_calls(buffers)
    if isinstance(calls, str):
        return StreamEvent(type=EventType.ERROR, error=calls, usage=usage)
    return StreamEvent(
        type=EventType.MESSAGE_COMPLETE,
        tool_calls=calls,
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        usage=usage,
    )


def output_text(response: object) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str):
        return direct
    fragments: list[str] = []
    for item in getattr(response, "output", ()):
        if getattr(item, "type", "") != "message":
            continue
        for content in getattr(item, "content", ()):
            if getattr(content, "type", "") == "output_text":
                fragments.append(getattr(content, "text", ""))
    return "".join(fragments)


def absorb_tool_calls(
    buffers: dict[int, ResponseToolBuffer],
    response: object,
) -> None:
    for index, item in enumerate(getattr(response, "output", ())):
        if getattr(item, "type", "") != "function_call":
            continue
        buffer = buffers.setdefault(index, ResponseToolBuffer())
        buffer.call_id = getattr(item, "call_id", "") or buffer.call_id
        buffer.name = getattr(item, "name", "") or buffer.name
        arguments = getattr(item, "arguments", "")
        if isinstance(arguments, str) and arguments:
            buffer.arguments = [arguments]


async def stream_response(client, request: dict[str, Any]):
    response = await client.responses.create(**request, stream=True)
    buffers: dict[int, ResponseToolBuffer] = {}

    async with response:
        async for event in response:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                yield StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta(content=getattr(event, "delta", "")),
                )
                continue

            if kind == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") != "function_call":
                    continue
                index = int(getattr(event, "output_index", 0))
                buffer = buffers.setdefault(index, ResponseToolBuffer())
                buffer.call_id = getattr(item, "call_id", "") or buffer.call_id
                buffer.name = getattr(item, "name", "") or buffer.name
                yield StreamEvent(
                    type=EventType.TOOL_CALL_DELTA,
                    tool_call_delta=ToolCallDelta(
                        index=index,
                        call_id=buffer.call_id or None,
                        name=buffer.name or None,
                    ),
                )
                continue

            if kind == "response.function_call_arguments.delta":
                index = int(getattr(event, "output_index", 0))
                delta = getattr(event, "delta", "")
                buffer = buffers.setdefault(index, ResponseToolBuffer())
                buffer.arguments.append(delta)
                yield StreamEvent(
                    type=EventType.TOOL_CALL_DELTA,
                    tool_call_delta=ToolCallDelta(
                        index=index,
                        arguments_delta=delta,
                    ),
                )
                continue

            if kind == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") != "function_call":
                    continue
                index = int(getattr(event, "output_index", 0))
                buffer = buffers.setdefault(index, ResponseToolBuffer())
                buffer.call_id = getattr(item, "call_id", "") or buffer.call_id
                buffer.name = getattr(item, "name", "") or buffer.name
                arguments = getattr(item, "arguments", "")
                if isinstance(arguments, str) and arguments:
                    buffer.arguments = [arguments]
                continue

            if kind == "response.completed":
                completed = getattr(event, "response", None)
                absorb_tool_calls(buffers, completed)
                yield complete_event(
                    buffers,
                    response_usage(getattr(completed, "usage", None)),
                )
                return

            if kind in {"response.failed", "response.incomplete"}:
                failed = getattr(event, "response", None)
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=response_error(failed),
                    usage=response_usage(getattr(failed, "usage", None)),
                )
                return

            if kind == "error":
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=getattr(event, "message", "The response stream failed."),
                )
                return

    yield complete_event(buffers, None)


async def non_stream_response(client, request: dict[str, Any]) -> StreamEvent:
    response = await client.responses.create(**request, stream=False)
    usage = response_usage(getattr(response, "usage", None))
    if getattr(response, "status", None) in {"failed", "incomplete", "cancelled"}:
        return StreamEvent(
            type=EventType.ERROR,
            error=response_error(response),
            usage=usage,
        )

    buffers: dict[int, ResponseToolBuffer] = {}
    absorb_tool_calls(buffers, response)

    event = complete_event(buffers, usage)
    text = output_text(response)
    if event.type == EventType.MESSAGE_COMPLETE and text:
        event.text_delta = TextDelta(content=text)
    return event
