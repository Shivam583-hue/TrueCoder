from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.models import ExecutionContext

ExecutionIdFactory = Callable[[], str]
UtcClock = Callable[[], datetime]

_WORKSPACE_ID_VERSION = 1


def workspace_id_for(project_root: Path) -> str:
    """Return a stable identity for one canonical host workspace."""

    canonical_root = _canonical_project_root(project_root)

    host_path = os.path.normcase(str(canonical_root))
    payload = f"truecoder-workspace-v{_WORKSPACE_ID_VERSION}\0{host_path}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"workspace_{digest}"


def _canonical_project_root(project_root: Path) -> Path:
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a pathlib.Path")

    try:
        canonical_root = project_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError("project_root must resolve to an existing path") from error

    if not canonical_root.is_dir():
        raise ValueError("project_root must be a directory")
    return canonical_root


class ExecutionContextFactory:
    """Construct execution contexts from caller supplied dependencies."""

    def __init__(
        self,
        *,
        execution_id_factory: ExecutionIdFactory | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        if execution_id_factory is not None and not callable(execution_id_factory):
            raise TypeError("execution_id_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        self._execution_id_factory = execution_id_factory or (
            lambda: f"exec_{uuid.uuid4().hex}"
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        *,
        tool_call_id: str,
        session_id: str,
        turn_id: str,
        project_root: Path,
    ) -> ExecutionContext:
        execution_id = self._required_identity(
            self._execution_id_factory(),
            "execution_id",
        )
        tool_call_id = self._required_identity(tool_call_id, "tool_call_id")
        session_id = self._required_identity(session_id, "session_id")
        turn_id = self._required_identity(turn_id, "turn_id")
        canonical_root = _canonical_project_root(project_root)

        return ExecutionContext(
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=workspace_id_for(canonical_root),
            project_root=canonical_root,
            launched_at_utc=self._clock(),
        )

    @staticmethod
    def _required_identity(value: object, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} cannot be empty")
        return normalized
