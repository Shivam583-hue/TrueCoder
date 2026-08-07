import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

import tiktoken
from dotenv import load_dotenv

from truecoder.agent.budget import fit_tool_messages, tool_result_ceiling
from truecoder.agent.messages import (
    ModelMessage,
    SystemMessage,
    copy_messages,
    create_system_message,
)
from truecoder.agent.prompts import (
    add_plan_tool_guidance,
    add_shell_tool_guidance,
    add_web_fetch_tool_guidance,
    build_system_prompt,
)
from truecoder.planning import PlanStore

if TYPE_CHECKING:
    from truecoder.agent.state import AgentState


class TokenCounter(Protocol):
    def count_message(self, message: Mapping[str, Any]) -> int: ...


class TiktokenTokenCounter:
    MESSAGE_OVERHEAD = 4
    _VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})

    def __init__(self, model: str) -> None:
        try:
            self.__encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            self.__encoding = tiktoken.get_encoding("o200k_base")

    def count_message(self, message: Mapping[str, Any]) -> int:
        self._validate_message(message)
        return self._count_string_values(message) + self.MESSAGE_OVERHEAD

    def _count_string_values(self, value: Any) -> int:
        if isinstance(value, str):
            return len(self.__encoding.encode(value))

        if value is None:
            return 0

        if isinstance(value, Mapping):
            return sum(
                self._count_string_values(nested_value)
                for nested_value in value.values()
            )

        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return sum(
                self._count_string_values(nested_value)
                for nested_value in value
            )

        raise TypeError("Message values must contain only strings, lists, mappings, or None.")

    @classmethod
    def _validate_message(cls, message: Mapping[str, Any]) -> None:
        role = message.get("role")
        if not isinstance(role, str):
            raise TypeError("Message role must be a string.")

        if role not in cls._VALID_ROLES:
            raise ValueError(f"Unsupported message role '{role}'.")

        content = message.get("content")

        if role in {"system", "user"}:
            if not isinstance(content, str):
                raise TypeError(f"{role.title()} message content must be a string.")
            return

        if role == "assistant":
            if content is not None and not isinstance(content, str):
                raise TypeError(
                    "Assistant message content must be a string or None."
                )

            if content is None and not isinstance(message.get("tool_calls"), list):
                raise TypeError(
                    "An assistant message without text must contain tool calls."
                )
            return

        if not isinstance(content, str):
            raise TypeError("Tool message content must be a string.")

        if not isinstance(message.get("tool_call_id"), str):
            raise TypeError("Tool messages require a string tool_call_id.")


class ContextBuilder:
    def __init__(
        self,
        system_prompt: str,
        max_input_tokens: int,
        token_counter: TokenCounter,
        plan_store: PlanStore | None = None,
        max_tool_result_tokens: int | None = None,
    ) -> None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("The system prompt cannot be empty.")

        if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int):
            raise TypeError("max_input_tokens must be an integer.")

        if max_input_tokens < 1:
            raise ValueError("max_input_tokens must be at least one.")

        if token_counter is None:
            raise ValueError("A token counter is required.")

        if plan_store is not None and not isinstance(plan_store, PlanStore):
            raise TypeError("plan_store must be a PlanStore.")

        if max_tool_result_tokens is not None and (
            isinstance(max_tool_result_tokens, bool)
            or not isinstance(max_tool_result_tokens, int)
            or max_tool_result_tokens < 1
        ):
            raise ValueError("max_tool_result_tokens must be a positive integer.")

        self.system_prompt = system_prompt.strip()
        self.max_input_tokens = max_input_tokens
        self.token_counter = token_counter
        self.plan_store = plan_store
        self.max_tool_result_tokens = (
            tool_result_ceiling(max_input_tokens)
            if max_tool_result_tokens is None
            else max_tool_result_tokens
        )

    @classmethod
    def from_environment(
        cls,
        *,
        project_instructions: str = "",
        plan_store: PlanStore | None = None,
    ) -> "ContextBuilder":
        load_dotenv()

        model = os.getenv("MODEL")
        if model is None or not model.strip():
            raise ValueError("The MODEL environment variable is required.")

        raw_max_tokens = os.getenv("MAX_INPUT_TOKENS", "12000")

        try:
            max_input_tokens = int(raw_max_tokens)
        except ValueError as error:
            raise ValueError(
                "MAX_INPUT_TOKENS must contain a valid integer."
            ) from error

        return cls(
            system_prompt=build_system_prompt(project_instructions),
            max_input_tokens=max_input_tokens,
            token_counter=TiktokenTokenCounter(model.strip()),
            plan_store=plan_store,
        )

    def build(self, state: "AgentState") -> list[ModelMessage]:
        if not state.turn_active:
            raise RuntimeError("Cannot build context without an active turn.")

        if state.outstanding_tool_call_ids:
            raise RuntimeError(
                "Cannot build context while tool calls remain unresolved."
            )

        if not state.pending_messages:
            raise RuntimeError("An active turn must contain pending messages.")

        pending_messages = self.project(state.pending_messages)

        system_message = create_system_message(self.system_prompt)
        plan_message = self.plan_message()
        plan_tail: list[ModelMessage] = [] if plan_message is None else [plan_message]
        required_messages: list[ModelMessage] = [
            system_message,
            *pending_messages,
            *plan_tail,
        ]
        context_token_count = sum(
            self.token_counter.count_message(message)
            for message in required_messages
        )

        if context_token_count > self.max_input_tokens:
            return copy_messages(required_messages)

        selected_turns: list[list[ModelMessage]] = []

        for turn in reversed(state.completed_turns):
            projected = self.project(turn)
            turn_token_count = sum(
                self.token_counter.count_message(message)
                for message in projected
            )

            if context_token_count + turn_token_count > self.max_input_tokens:
                break

            selected_turns.append(projected)
            context_token_count += turn_token_count

        selected_history = [
            message
            for turn in reversed(selected_turns)
            for message in turn
        ]

        return copy_messages(
            [
                system_message,
                *selected_history,
                *pending_messages,
                *plan_tail,
            ]
        )

    def project(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        return fit_tool_messages(
            messages,
            self.token_counter,
            self.max_tool_result_tokens,
        )

    def plan_message(self) -> SystemMessage | None:
        if self.plan_store is None:
            return None

        plan = self.plan_store.current
        if plan is None:
            return None

        return create_system_message(plan.render())

    def attach_plan_store(self, plan_store: PlanStore) -> None:
        if not isinstance(plan_store, PlanStore):
            raise TypeError("plan_store must be a PlanStore.")
        self.plan_store = plan_store

    def enable_shell_tool(self) -> None:
        self.system_prompt = add_shell_tool_guidance(self.system_prompt)

    def enable_plan_tool(self) -> None:
        self.system_prompt = add_plan_tool_guidance(self.system_prompt)

    def enable_web_fetch_tool(self) -> None:
        self.system_prompt = add_web_fetch_tool_guidance(self.system_prompt)
