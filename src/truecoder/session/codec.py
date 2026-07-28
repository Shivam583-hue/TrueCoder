from __future__ import annotations

import json
from collections.abc import Sequence

from truecoder.agent.messages import ModelMessage
from truecoder.agent.state import AgentState
from truecoder.session.models import SessionFormatError

TURN_FORMAT_VERSION = 1


def encode_turn(turn: Sequence[ModelMessage]) -> str:
    """Validate and encode one completed turn."""
    state = AgentState()
    try:
        state.replace_completed_turns([turn])
    except (TypeError, ValueError, RuntimeError) as error:
        raise SessionFormatError(f"Cannot encode completed turn: {error}") from error

    payload = {
        "version": TURN_FORMAT_VERSION,
        "messages": state.completed_turns[0],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_turn(payload: str) -> tuple[ModelMessage, ...]:
    """Decode and validate one persisted completed turn."""
    if not isinstance(payload, str):
        raise SessionFormatError("Persisted turn payload must be a string.")

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise SessionFormatError("Persisted turn is not valid JSON.") from error

    if not isinstance(decoded, dict):
        raise SessionFormatError("Persisted turn must be a JSON object.")
    if decoded.get("version") != TURN_FORMAT_VERSION:
        raise SessionFormatError("Persisted turn uses an unsupported version.")

    messages = decoded.get("messages")
    if not isinstance(messages, list):
        raise SessionFormatError("Persisted turn messages must be a list.")

    state = AgentState()
    try:
        state.replace_completed_turns([messages])
    except (TypeError, ValueError, RuntimeError) as error:
        raise SessionFormatError(f"Persisted turn is invalid: {error}") from error

    return tuple(state.completed_turns[0])
