from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
)
from truecoder.providers.models import Credential, Provider
from truecoder.tools.base import ToolCall

ANTHROPIC_VERSION: Final = "2023-06-01"
ANTHROPIC_MAX_TOKENS: Final = 16384
REQUEST_TIMEOUT_SECONDS: Final = 300.0


@dataclass(frozen=True, slots=True)
class NativeProviderError(RuntimeError):
    status_code: int
    body: Any

    def __str__(self) -> str:
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        return f"provider returned {self.status_code}"


def _secret(credential: Credential) -> str:
    value = credential.client_options().get("api_key", "")
    return value if isinstance(value, str) else ""


def _tool_definitions(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        item["function"]
        for item in tools or []
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    ]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _function(call: object) -> Mapping[str, Any]:
    if not isinstance(call, Mapping):
        return {}
    value = call.get("function")
    return value if isinstance(value, Mapping) else {}


def _arguments(value: object) -> object:
    if not isinstance(value, str):
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return {}


def _append_content(
    messages: list[dict[str, Any]],
    role: str,
    content: list[dict[str, Any]],
) -> None:
    if not content:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
        return
    messages.append({"role": role, "content": content})


def anthropic_request(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    stream: bool,
) -> dict[str, Any]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            value = _text(message.get("content"))
            if value:
                system.append(value)
            continue
        if role == "user":
            _append_content(
                converted,
                "user",
                [{"type": "text", "text": _text(message.get("content"))}],
            )
            continue
        if role == "assistant":
            content: list[dict[str, Any]] = []
            value = _text(message.get("content"))
            if value:
                content.append({"type": "text", "text": value})
            calls = message.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                function = _function(call)
                name = function.get("name")
                call_id = call.get("id") if isinstance(call, Mapping) else None
                if not isinstance(name, str) or not isinstance(call_id, str):
                    continue
                content.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": _arguments(function.get("arguments")),
                    }
                )
            _append_content(converted, "assistant", content)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str):
                _append_content(
                    converted,
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": _text(message.get("content")),
                        }
                    ],
                )

    request: dict[str, Any] = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "messages": converted,
        "stream": stream,
    }
    if system:
        request["system"] = "\n\n".join(system)
    definitions = _tool_definitions(tools)
    if definitions:
        request["tools"] = [
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "input_schema": item.get("parameters", {"type": "object"}),
            }
            for item in definitions
        ]
    return request


def _call_names(messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            function = _function(call)
            call_id = call.get("id") if isinstance(call, Mapping) else None
            name = function.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
    return names


def google_request(
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    names = _call_names(messages)
    for message in messages:
        role = message.get("role")
        if role == "system":
            value = _text(message.get("content"))
            if value:
                system.append(value)
            continue
        if role in {"user", "assistant"}:
            parts: list[dict[str, Any]] = []
            value = _text(message.get("content"))
            if value:
                parts.append({"text": value})
            calls = message.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                function = _function(call)
                name = function.get("name")
                if isinstance(name, str):
                    parts.append(
                        {
                            "functionCall": {
                                "name": name,
                                "args": _arguments(function.get("arguments")),
                            }
                        }
                    )
            if parts:
                converted.append(
                    {"role": "model" if role == "assistant" else "user", "parts": parts}
                )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            name = names.get(call_id, "tool") if isinstance(call_id, str) else "tool"
            converted.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                "response": {"result": _text(message.get("content"))},
                            }
                        }
                    ],
                }
            )

    request: dict[str, Any] = {"contents": converted}
    if system:
        request["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system)}]
        }
    definitions = _tool_definitions(tools)
    if definitions:
        request["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": item.get("name", ""),
                        "description": item.get("description", ""),
                        "parameters": item.get("parameters", {"type": "object"}),
                    }
                    for item in definitions
                ]
            }
        ]
    return request


async def _sse(lines: AsyncIterator[str]) -> AsyncGenerator[dict[str, Any], None]:
    async for line in lines:
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if not value or value == "[DONE]":
            continue
        try:
            event = json.loads(value)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _usage(
    prompt: object,
    completion: object,
    cached: object = 0,
) -> TokenUsage:
    input_tokens = prompt if isinstance(prompt, int) else 0
    output_tokens = completion if isinstance(completion, int) else 0
    cached_tokens = cached if isinstance(cached, int) else 0
    return TokenUsage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_tokens=cached_tokens,
    )


async def _anthropic_stream(response) -> AsyncGenerator[StreamEvent, None]:
    buffers: dict[int, dict[str, str]] = {}
    usage = TokenUsage()
    finish_reason: str | None = None
    async for event in _sse(response.aiter_lines()):
        kind = event.get("type")
        if kind == "message_start":
            raw = event.get("message")
            raw_usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            usage = _usage(
                raw_usage.get("input_tokens"),
                0,
                raw_usage.get("cache_read_input_tokens"),
            )
        elif kind == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                if isinstance(call_id, str) and isinstance(name, str):
                    buffers[index] = {"id": call_id, "name": name, "arguments": ""}
                    yield StreamEvent(
                        type=EventType.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(index, call_id, name, ""),
                    )
        elif kind == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                yield StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta(delta["text"]),
                )
            elif (
                delta.get("type") == "input_json_delta"
                and isinstance(index, int)
                and index in buffers
            ):
                fragment = delta.get("partial_json")
                if isinstance(fragment, str):
                    buffers[index]["arguments"] += fragment
                    yield StreamEvent(
                        type=EventType.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index,
                            arguments_delta=fragment,
                        ),
                    )
        elif kind == "message_delta":
            delta = event.get("delta")
            raw_usage = event.get("usage")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                finish_reason = delta["stop_reason"]
            if isinstance(raw_usage, dict):
                usage.completion_tokens = int(raw_usage.get("output_tokens", 0) or 0)
                usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

    calls = tuple(
        ToolCall(item["id"], item["name"], item["arguments"] or "{}")
        for _, item in sorted(buffers.items())
    )
    yield StreamEvent(
        type=EventType.MESSAGE_COMPLETE,
        tool_calls=calls,
        finish_reason="tool_calls" if finish_reason == "tool_use" else finish_reason,
        usage=usage,
    )


def _google_parts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return [], None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return [], None
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else []
    return (
        [part for part in parts if isinstance(part, dict)]
        if isinstance(parts, list)
        else [],
        candidate.get("finishReason")
        if isinstance(candidate.get("finishReason"), str)
        else None,
    )


async def _google_stream(response) -> AsyncGenerator[StreamEvent, None]:
    calls: list[ToolCall] = []
    usage = TokenUsage()
    finish_reason: str | None = None
    async for event in _sse(response.aiter_lines()):
        parts, stopped = _google_parts(event)
        finish_reason = stopped or finish_reason
        for part in parts:
            text = part.get("text")
            if isinstance(text, str) and text and not part.get("thought"):
                yield StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta(text),
                )
            function = part.get("functionCall")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                index = len(calls)
                call_id = function.get("id")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call_{index + 1}"
                arguments = json.dumps(
                    function.get("args", {}),
                    separators=(",", ":"),
                )
                calls.append(ToolCall(call_id, function["name"], arguments))
                yield StreamEvent(
                    type=EventType.TOOL_CALL_DELTA,
                    tool_call_delta=ToolCallDelta(
                        index,
                        call_id,
                        function["name"],
                        arguments,
                    ),
                )
        raw_usage = event.get("usageMetadata")
        if isinstance(raw_usage, dict):
            usage = _usage(
                raw_usage.get("promptTokenCount"),
                raw_usage.get("candidatesTokenCount"),
                raw_usage.get("cachedContentTokenCount"),
            )
    yield StreamEvent(
        type=EventType.MESSAGE_COMPLETE,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else (finish_reason or "stop").lower(),
        usage=usage,
    )


async def _error(response) -> NativeProviderError:
    raw = await response.aread()
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        body = raw.decode("utf-8", errors="replace")
    return NativeProviderError(response.status_code, body)


async def anthropic_completion(
    provider: Provider,
    credential: Credential,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    stream: bool,
) -> AsyncGenerator[StreamEvent, None]:
    import httpx

    root = (provider.base_url or "https://api.anthropic.com/v1").rstrip("/")
    headers = {
        **provider.headers,
        **credential.request_headers(),
        "anthropic-version": ANTHROPIC_VERSION,
        "x-api-key": _secret(credential),
    }
    request = anthropic_request(model, messages, tools, stream=stream)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        if stream:
            async with client.stream(
                "POST",
                f"{root}/messages",
                headers=headers,
                json=request,
            ) as response:
                if not response.is_success:
                    raise await _error(response)
                async for event in _anthropic_stream(response):
                    yield event
            return
        response = await client.post(f"{root}/messages", headers=headers, json=request)
        if not response.is_success:
            raise await _error(response)
        payload = response.json()
        content = payload.get("content", []) if isinstance(payload, dict) else []
        text = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
        calls = tuple(
            ToolCall(
                item["id"],
                item["name"],
                json.dumps(item.get("input", {}), separators=(",", ":")),
            )
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "tool_use"
            and isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
        )
        raw_usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=TextDelta(text) if text else None,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else payload.get("stop_reason"),
            usage=_usage(
                raw_usage.get("input_tokens"),
                raw_usage.get("output_tokens"),
                raw_usage.get("cache_read_input_tokens"),
            ),
        )


async def google_completion(
    provider: Provider,
    credential: Credential,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    stream: bool,
) -> AsyncGenerator[StreamEvent, None]:
    import httpx

    root = (
        provider.base_url or "https://generativelanguage.googleapis.com/v1beta"
    ).rstrip("/")
    action = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = f"{root}/models/{quote(model, safe='')}:{action}"
    headers = {
        **provider.headers,
        **credential.request_headers(),
        "x-goog-api-key": _secret(credential),
    }
    request = google_request(messages, tools)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        if stream:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=request,
            ) as response:
                if not response.is_success:
                    raise await _error(response)
                async for event in _google_stream(response):
                    yield event
            return
        response = await client.post(url, headers=headers, json=request)
        if not response.is_success:
            raise await _error(response)
        payload = response.json()
        parts, finish_reason = _google_parts(payload)
        text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part.get("text"), str) and not part.get("thought")
        )
        calls: list[ToolCall] = []
        for part in parts:
            function = part.get("functionCall")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            index = len(calls)
            call_id = function.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"call_{index + 1}"
            calls.append(
                ToolCall(
                    call_id,
                    function["name"],
                    json.dumps(function.get("args", {}), separators=(",", ":")),
                )
            )
        raw_usage = payload.get("usageMetadata", {})
        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=TextDelta(text) if text else None,
            tool_calls=tuple(calls),
            finish_reason="tool_calls" if calls else (finish_reason or "stop").lower(),
            usage=_usage(
                raw_usage.get("promptTokenCount"),
                raw_usage.get("candidatesTokenCount"),
                raw_usage.get("cachedContentTokenCount"),
            ),
        )


async def native_completion(
    provider: Provider,
    credential: Credential,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    stream: bool,
) -> AsyncGenerator[StreamEvent, None]:
    if provider.adapter == "anthropic":
        async for event in anthropic_completion(
            provider,
            credential,
            model,
            messages,
            tools,
            stream=stream,
        ):
            yield event
        return
    if provider.adapter == "google":
        async for event in google_completion(
            provider,
            credential,
            model,
            messages,
            tools,
            stream=stream,
        ):
            yield event
        return
    raise RuntimeError(f"No native transport exists for {provider.adapter!r}.")
