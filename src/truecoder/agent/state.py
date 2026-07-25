from collections.abc import Sequence

from truecoder.agent.messages import (
    ModelMessage,
    copy_messages,
    create_assistant_text_message,
    create_assistant_tool_call_message,
    create_tool_message,
    create_user_message,
)
from truecoder.tools.base import ToolCall


class AgentState:
    """Transactional conversation state for completed and in-progress turns."""

    def __init__(self) -> None:
        self.__completed_turns: list[list[ModelMessage]] = []
        self.__pending_messages: list[ModelMessage] | None = None
        self.__outstanding_tool_call_ids: list[str] = []
        self.__seen_tool_call_ids: set[str] = set()

    @property
    def messages(self) -> list[ModelMessage]:
        """Return a flattened defensive copy of completed history."""

        return copy_messages(
            [
                message
                for turn in self.__completed_turns
                for message in turn
            ]
        )

    @property
    def completed_turns(self) -> list[list[ModelMessage]]:
        """Return completed turns while preserving their boundaries."""

        return [copy_messages(turn) for turn in self.__completed_turns]

    @property
    def pending_messages(self) -> list[ModelMessage]:
        """Return a defensive copy of the active turn."""

        if self.__pending_messages is None:
            return []

        return copy_messages(self.__pending_messages)

    @property
    def pending_prompt(self) -> str | None:
        if self.__pending_messages is None:
            return None

        first_message = self.__pending_messages[0]
        if first_message["role"] != "user":
            raise RuntimeError("An active turn must begin with a user message.")

        return first_message["content"]

    @property
    def outstanding_tool_call_ids(self) -> tuple[str, ...]:
        return tuple(self.__outstanding_tool_call_ids)

    @property
    def turn_active(self) -> bool:
        return self.__pending_messages is not None

    def begin_turn(self, prompt: str) -> None:
        prompt = prompt.strip()

        if not prompt:
            raise ValueError("The prompt cannot be empty.")

        if self.turn_active:
            raise RuntimeError("A turn is already active.")

        self.__pending_messages = [create_user_message(prompt)]
        self.__outstanding_tool_call_ids.clear()
        self.__seen_tool_call_ids.clear()

    def record_tool_calls(
        self,
        tool_calls: Sequence[ToolCall],
        content: str | None = None,
    ) -> None:
        if self.__pending_messages is None:
            raise RuntimeError("There is no active turn to record tool calls.")

        if content is not None and not isinstance(content, str):
            raise TypeError("Assistant tool-call content must be a string or None.")

        calls = list(tool_calls)
        if not calls:
            raise ValueError("At least one tool call is required.")

        if not all(isinstance(tool_call, ToolCall) for tool_call in calls):
            raise TypeError("tool_calls must contain only ToolCall instances.")

        if self.__outstanding_tool_call_ids:
            raise RuntimeError("Earlier tool calls remain unresolved.")

        call_ids = [tool_call.call_id for tool_call in calls]

        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Tool call IDs must be unique within a batch.")

        if any(call_id in self.__seen_tool_call_ids for call_id in call_ids):
            raise ValueError("Tool call IDs cannot be reused within a turn.")

        self.__pending_messages.append(
            create_assistant_tool_call_message(calls, content),
        )
        self.__outstanding_tool_call_ids.extend(call_ids)
        self.__seen_tool_call_ids.update(call_ids)

    def record_tool_result(
        self,
        call_id: str,
        content: str,
    ) -> None:
        if self.__pending_messages is None:
            raise RuntimeError("There is no active turn to record a tool result.")

        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("Tool result call ID cannot be empty.")

        if not isinstance(content, str) or not content:
            raise ValueError("Tool result content cannot be empty.")

        if not self.__outstanding_tool_call_ids:
            raise RuntimeError("There are no outstanding tool calls.")

        expected_call_id = self.__outstanding_tool_call_ids[0]
        if call_id != expected_call_id:
            raise ValueError(
                "Tool calls must be resolved in order. "
                f"Expected '{expected_call_id}', received '{call_id}'."
            )

        self.__pending_messages.append(
            create_tool_message(call_id, content),
        )
        self.__outstanding_tool_call_ids.pop(0)

    def complete_turn(self, response: str) -> None:
        if self.__pending_messages is None:
            raise RuntimeError("There is no active turn to complete.")

        if self.__outstanding_tool_call_ids:
            raise RuntimeError(
                "The turn cannot complete while tool calls remain unresolved."
            )

        self.__pending_messages.append(
            create_assistant_text_message(response),
        )
        self.__completed_turns.append(self.__pending_messages)
        self.__clear_pending_state()

    def abort_turn(self) -> None:
        self.__clear_pending_state()

    def messages_for_context(self) -> list[ModelMessage]:
        context_messages = self.messages
        if self.__pending_messages is not None:
            context_messages.extend(copy_messages(self.__pending_messages))

        return context_messages

    def reset(self) -> None:
        self.__completed_turns.clear()
        self.__clear_pending_state()

    def __clear_pending_state(self) -> None:
        self.__pending_messages = None
        self.__outstanding_tool_call_ids.clear()
        self.__seen_tool_call_ids.clear()
