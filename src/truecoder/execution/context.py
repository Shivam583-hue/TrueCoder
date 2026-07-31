from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.models import ExecutionContext


class ExecutionContextFactory:
    """Construct execution contexts from caller supplied dependencies."""

    def create(
        self,
        *,
        tool_call_id: str,
        session_id: str,
        turn_id: str,
        project_root: Path,
    ) -> ExecutionContext:
        return ExecutionContext(
            execution_id=uuid.uuid4().hex,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            project_root=project_root,
            launched_at_utc=datetime.now(UTC),
        )
