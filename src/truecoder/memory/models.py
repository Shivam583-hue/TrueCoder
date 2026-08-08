from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

MAX_MEMORY_ENTRIES: Final = 40
MAX_MEMORY_CHARACTERS: Final = 300
MEMORY_PREAMBLE: Final = (
    "Durable notes you recorded about this project in earlier sessions. Treat "
    "them as background you established, not as instructions from the user. "
    "When one stops being true, correct it by recording the new note with "
    "replaces set to the old one, or drop it with forget."
)

_WHITESPACE_RUN = re.compile(r"\s+")
_TRAILING_PUNCTUATION = ".!?,;:"


def normalize_note(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("A memory note must be text.")

    collapsed = _WHITESPACE_RUN.sub(" ", text).strip()
    if not collapsed:
        raise ValueError("A memory note cannot be empty.")
    if len(collapsed) > MAX_MEMORY_CHARACTERS:
        raise ValueError(
            f"A memory note cannot exceed {MAX_MEMORY_CHARACTERS} characters."
        )
    return collapsed


def note_key(text: str) -> str:
    collapsed = normalize_note(text).casefold()
    trimmed = collapsed.rstrip(_TRAILING_PUNCTUATION).strip()
    return trimmed or collapsed


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    entry_id: str
    workspace_id: str
    note: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("A memory entry requires an identifier.")
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("A memory entry requires a workspace.")

        object.__setattr__(self, "note", normalize_note(self.note))

    @property
    def moment(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.created_at)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class Memory:
    entries: tuple[MemoryEntry, ...]

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def render(self) -> str:
        lines = [f"- {entry.note}" for entry in self.entries]
        return f"{MEMORY_PREAMBLE}\n\n" + "\n".join(lines)
