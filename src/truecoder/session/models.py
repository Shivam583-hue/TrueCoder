from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from truecoder.agent.messages import ModelMessage


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    project_root: Path
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int
    title_is_custom: bool


@dataclass(frozen=True, slots=True)
class SessionRecord:
    summary: SessionSummary
    completed_turns: tuple[tuple[ModelMessage, ...], ...]


class SessionError(RuntimeError):
    """Base error for session operations."""


class SessionNotFoundError(SessionError):
    """The requested session does not exist in the current project."""


class SessionStorageError(SessionError):
    """Session data could not be stored or retrieved."""


class SessionFormatError(SessionError):
    """Persisted session data does not match the supported format."""
