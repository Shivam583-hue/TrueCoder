from typing import Any

Message = dict[str, Any]


class AgentState:
    def __init__(self) -> None:
        self.__completed_messages: list[Message] = []
        self.__pending_prompt: str | None = None

    @property
    def messages(self) -> list[Message]:
        return [message.copy() for message in self.__completed_messages]

    @property
    def pending_prompt(self) -> str | None:
        return self.__pending_prompt

    @property
    def turn_active(self) -> bool:
        return self.__pending_prompt is not None

    def begin_turn(self, prompt: str) -> None:
        prompt = prompt.strip()

        if not prompt:
            raise ValueError("The prompt cannot be empty.")

        if self.turn_active:
            raise RuntimeError("A turn is already active.")

        self.__pending_prompt = prompt

    def complete_turn(self, response: str) -> None:
        if self.__pending_prompt is None:
            raise RuntimeError("There is no active turn to complete.")

        self.__completed_messages.extend(
            [
                {
                    "role": "user",
                    "content": self.__pending_prompt,
                },
                {
                    "role": "assistant",
                    "content": response,
                },
            ]
        )

        self.__pending_prompt = None

    def abort_turn(self) -> None:
        self.__pending_prompt = None

    def messages_for_context(self) -> list[Message]:
        context_messages = [message.copy() for message in self.__completed_messages]

        if self.__pending_prompt is not None:
            context_messages.append(
                {
                    "role": "user",
                    "content": self.__pending_prompt,
                }
            )

        return context_messages

    def reset(self) -> None:
        self.__completed_messages.clear()
        self.__pending_prompt = None
