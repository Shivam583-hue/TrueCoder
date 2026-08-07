from __future__ import annotations

from typing import TypedDict

from pydantic import Field

from truecoder.memory import MAX_MEMORY_CHARACTERS, MemoryStore
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.context import ToolInvocationContext


class RememberArguments(ToolArguments):
    note: str = Field(
        min_length=1,
        max_length=MAX_MEMORY_CHARACTERS,
        description=(
            "One durable fact about this project, written so it still makes "
            "sense weeks from now."
        ),
    )


class ForgetArguments(ToolArguments):
    note: str = Field(
        min_length=1,
        max_length=MAX_MEMORY_CHARACTERS,
        description="The exact note to drop, as it appears in your memory.",
    )


class RememberOutput(TypedDict):
    note: str
    stored: int


class ForgetOutput(TypedDict):
    note: str
    removed: bool
    stored: int


class _MemoryTool(BaseTool[ToolArguments]):
    approval = ToolApproval.REQUIRED

    def __init__(self, store: MemoryStore) -> None:
        if not isinstance(store, MemoryStore):
            raise TypeError("store must be a MemoryStore.")

        self._store = store

    @property
    def store(self) -> MemoryStore:
        return self._store


class RememberTool(_MemoryTool):
    name = "remember"
    description = (
        "Record one durable fact about this project so it is available in later "
        "sessions. Use it for things that stay true: where a subsystem lives, a "
        "convention the user asked for, a decision and its reason. Do not record "
        "transient state, secrets, or anything already written in the repository."
    )
    arguments_type = RememberArguments

    async def run(
        self,
        arguments: RememberArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> RememberOutput:
        del invocation

        try:
            entry = self._store.remember(arguments.note)
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(str(error), code="invalid_note") from error
        except Exception as error:
            raise ToolExecutionError(
                "The note could not be recorded.",
                code="memory_unavailable",
            ) from error

        return {"note": entry.note, "stored": len(self._store.entries())}


class ForgetTool(_MemoryTool):
    name = "forget"
    description = (
        "Drop one note you previously recorded, when it has become wrong or "
        "irrelevant. Quote the note exactly as it appears in your memory."
    )
    arguments_type = ForgetArguments

    async def run(
        self,
        arguments: ForgetArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> ForgetOutput:
        del invocation

        try:
            removed = self._store.forget_note(arguments.note)
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(str(error), code="invalid_note") from error
        except Exception as error:
            raise ToolExecutionError(
                "The note could not be removed.",
                code="memory_unavailable",
            ) from error

        return {
            "note": arguments.note,
            "removed": removed,
            "stored": len(self._store.entries()),
        }


def memory_tools(store: MemoryStore) -> tuple[BaseTool[ToolArguments], ...]:
    return (ForgetTool(store), RememberTool(store))
