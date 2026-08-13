from __future__ import annotations

from typing import TypedDict

from pydantic import Field
from typing_extensions import NotRequired

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
    replaces: str | None = Field(
        default=None,
        max_length=MAX_MEMORY_CHARACTERS,
        description=(
            "The note this one supersedes, quoted from your memory. Setting it "
            "removes that note as this one is recorded, so a correction leaves "
            "one note rather than two that disagree."
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
    replaced: NotRequired[str]


class ForgetOutput(TypedDict):
    note: str
    removed: bool
    stored: int
    available: NotRequired[list[str]]


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
        "transient state, secrets, or anything already written in the repository. "
        "To correct a note that has stopped being true, record the new one with "
        "replaces set to the old one rather than recording it on its own."
    )
    arguments_type = RememberArguments

    async def run(
        self,
        arguments: RememberArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> RememberOutput:
        del invocation

        superseded = self._superseded(arguments.replaces)

        try:
            entry = self._store.remember(arguments.note, replaces=arguments.replaces)
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(str(error), code="invalid_note") from error
        except Exception as error:
            raise ToolExecutionError(
                "The note could not be recorded.",
                code="memory_unavailable",
            ) from error

        result: RememberOutput = {
            "note": entry.note,
            "stored": len(self._store.entries()),
        }
        if superseded is not None:
            result["replaced"] = superseded
        return result

    def _superseded(self, replaces: str | None) -> str | None:
        if replaces is None:
            return None
        try:
            existing = self._store.find(replaces)
        except Exception:  # noqa: BLE001 - reporting never blocks the write
            return None
        return None if existing is None else existing.note


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

        remaining = self._store.entries()
        result: ForgetOutput = {
            "note": arguments.note,
            "removed": removed,
            "stored": len(remaining),
        }
        if not removed:
            result["available"] = [entry.note for entry in remaining]
        return result


def memory_tools(store: MemoryStore) -> tuple[BaseTool[ToolArguments], ...]:
    return (ForgetTool(store), RememberTool(store))
