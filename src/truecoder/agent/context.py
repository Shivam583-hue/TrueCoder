import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

import tiktoken
from dotenv import load_dotenv

from truecoder.agent.prompts import DEFAULT_SYSTEM_PROMPT

if TYPE_CHECKING:
    from truecoder.agent.state import AgentState


Message = dict[str, str]


class TokenCounter(Protocol):
    def count_message(self, message: Mapping[str, str]) -> int: ...


class TiktokenTokenCounter:
    MESSAGE_OVERHEAD = 4

    def __init__(self, model: str) -> None:
        try:
            self.__encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            self.__encoding = tiktoken.get_encoding("o200k_base")

    def count_message(self, message: Mapping[str, str]) -> int:
        role = message.get("role")
        content = message.get("content")

        if not isinstance(role, str):
            raise TypeError("Message role must be a string.")

        if not isinstance(content, str):
            raise TypeError("Message content must be a string.")

        role_tokens = len(self.__encoding.encode(role))
        content_tokens = len(self.__encoding.encode(content))

        return role_tokens + content_tokens + self.MESSAGE_OVERHEAD


class ContextBuilder:
    def __init__(
        self,
        system_prompt: str,
        max_input_tokens: int,
        token_counter: TokenCounter,
    ) -> None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("The system prompt cannot be empty.")

        if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int):
            raise TypeError("max_input_tokens must be an integer.")

        if max_input_tokens < 1:
            raise ValueError("max_input_tokens must be at least one.")

        if token_counter is None:
            raise ValueError("A token counter is required.")

        self.system_prompt = system_prompt.strip()
        self.max_input_tokens = max_input_tokens
        self.token_counter = token_counter

    @classmethod
    def from_environment(cls) -> "ContextBuilder":
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
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            max_input_tokens=max_input_tokens,
            token_counter=TiktokenTokenCounter(model.strip()),
        )

    def build(self, state: "AgentState") -> list[Message]:
        if not state.turn_active:
            raise RuntimeError("Cannot build context without an active turn.")

        pending_prompt = state.pending_prompt
        if pending_prompt is None:
            raise RuntimeError("An active turn must have a pending prompt.")

        system_message: Message = {
            "role": "system",
            "content": self.system_prompt,
        }
        current_user_message: Message = {
            "role": "user",
            "content": pending_prompt,
        }

        context_token_count = self.token_counter.count_message(
            system_message
        ) + self.token_counter.count_message(current_user_message)

        if context_token_count > self.max_input_tokens:
            return [system_message, current_user_message]

        completed_messages = state.messages

        if len(completed_messages) % 2 != 0:
            raise RuntimeError(
                "Completed message history must contain user/assistant pairs."
            )

        selected_pairs: list[list[Message]] = []

        for index in range(len(completed_messages) - 2, -1, -2):
            user_message = completed_messages[index]
            assistant_message = completed_messages[index + 1]

            if (
                user_message.get("role") != "user"
                or assistant_message.get("role") != "assistant"
            ):
                raise RuntimeError(
                    "Completed history contains an invalid message pair."
                )

            pair_token_count = self.token_counter.count_message(
                user_message
            ) + self.token_counter.count_message(assistant_message)

            if context_token_count + pair_token_count > self.max_input_tokens:
                break

            selected_pairs.append([user_message, assistant_message])
            context_token_count += pair_token_count

        selected_history: list[Message] = []

        for pair in reversed(selected_pairs):
            selected_history.extend(message.copy() for message in pair)

        return [
            system_message,
            *selected_history,
            current_user_message,
        ]
