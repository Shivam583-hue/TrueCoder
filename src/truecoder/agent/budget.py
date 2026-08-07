from __future__ import annotations

import json
from typing import Final

from truecoder.agent.messages import ModelMessage, ToolMessage, create_tool_message

TOOL_RESULT_BUDGET_SHARE: Final = 4
MIN_TOOL_RESULT_TOKENS: Final = 256
TRUNCATION_NOTE: Final = (
    "This result was shortened to fit the context budget. Request a narrower "
    "range if you need the omitted part."
)


def tool_result_ceiling(
    max_input_tokens: int,
    *,
    share: int = TOOL_RESULT_BUDGET_SHARE,
    minimum: int = MIN_TOOL_RESULT_TOKENS,
) -> int:
    if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int):
        raise TypeError("max_input_tokens must be an integer")
    if max_input_tokens < 1:
        raise ValueError("max_input_tokens must be at least one")
    if isinstance(share, bool) or not isinstance(share, int) or share < 1:
        raise ValueError("share must be a positive integer")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("minimum must be a positive integer")

    return max(minimum, max_input_tokens // share)


def _split_payload(content: str) -> tuple[str, str]:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return "unknown", content

    if not isinstance(payload, dict):
        return "unknown", content

    status = payload.get("status")
    body = payload.get("output", payload.get("error"))
    if body is None:
        return str(status or "unknown"), content

    rendered = (
        body
        if isinstance(body, str)
        else json.dumps(body, ensure_ascii=False, sort_keys=True)
    )
    return str(status or "unknown"), rendered


def _envelope(status: str, kept: str, omitted: int) -> str:
    return json.dumps(
        {
            "status": status,
            "truncated": True,
            "omitted_characters": omitted,
            "note": TRUNCATION_NOTE,
            "output": kept,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def fit_tool_message(
    message: ToolMessage,
    counter,
    ceiling: int,
) -> ToolMessage:
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        raise ValueError("ceiling must be a positive integer")

    if counter.count_message(message) <= ceiling:
        return message

    call_id = message["tool_call_id"]
    status, body = _split_payload(message["content"])

    low = 0
    high = len(body)
    best = _envelope(status, "", len(body))

    while low <= high:
        middle = (low + high) // 2
        candidate = _envelope(status, body[:middle], len(body) - middle)
        if counter.count_message(create_tool_message(call_id, candidate)) <= ceiling:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1

    return create_tool_message(call_id, best)


def fit_tool_messages(
    messages: list[ModelMessage],
    counter,
    ceiling: int,
) -> list[ModelMessage]:
    return [
        fit_tool_message(message, counter, ceiling)  # type: ignore[arg-type]
        if message.get("role") == "tool"
        else message
        for message in messages
    ]
