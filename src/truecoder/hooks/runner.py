from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.errors import ExecutionInfrastructureError
from truecoder.execution.models import ExecutionLimits, ExecutionRequest
from truecoder.hooks.models import Hook, HookOutcome

MAX_HOOK_OUTPUT_BYTES: Final = 256 * 1024
MAX_HOOK_RETURN_BYTES: Final = 8 * 1024
MAX_DETAIL_CHARACTERS: Final = 500


def hook_limits(hook: Hook) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=hook.timeout_seconds,
        max_output_bytes=MAX_HOOK_OUTPUT_BYTES,
        max_return_bytes=MAX_HOOK_RETURN_BYTES,
        memory_bytes=DEFAULT_EXECUTION_LIMITS.memory_bytes,
        cpu_seconds=DEFAULT_EXECUTION_LIMITS.cpu_seconds,
        max_processes=DEFAULT_EXECUTION_LIMITS.max_processes,
    )


def resolve_working_directory(project_root: Path, requested: str) -> Path:
    candidate = Path(requested)
    if candidate.is_absolute():
        raise ValueError("a hook working directory must be workspace-relative")

    resolved = (project_root / candidate).resolve()
    root = project_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("a hook working directory must stay inside the workspace")
    return resolved


def build_hook_request(hook: Hook, project_root: Path) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=tuple(hook.command),
        script=None,
        working_directory=resolve_working_directory(
            project_root,
            hook.working_directory,
        ),
        limits=hook_limits(hook),
        network_access=True,
        filesystem_mode="host",
        backend="local",
        shell_kind="auto",
    )


class HookRunner:
    def __init__(
        self,
        service,
        project_root: Path,
        *,
        context_factory,
        pre_authorise=None,
    ) -> None:
        self._service = service
        self._project_root = project_root.resolve()
        self._context_factory = context_factory
        self._pre_authorise = pre_authorise

    async def run(
        self,
        hooks: Sequence[Hook],
        *,
        session_id: str,
        turn_id: str,
    ) -> tuple[HookOutcome, ...]:
        outcomes: list[HookOutcome] = []
        for hook in hooks:
            outcomes.append(
                await self._run_one(hook, session_id=session_id, turn_id=turn_id)
            )
        return tuple(outcomes)

    async def _run_one(
        self,
        hook: Hook,
        *,
        session_id: str,
        turn_id: str,
    ) -> HookOutcome:
        try:
            request = build_hook_request(hook, self._project_root)
        except (TypeError, ValueError) as error:
            return HookOutcome(
                hook=hook,
                status="refused",
                detail=str(error)[:MAX_DETAIL_CHARACTERS],
            )

        context = self._context_factory.create(
            tool_call_id=f"hook_{hook.name.replace(' ', '_')}",
            session_id=session_id,
            turn_id=turn_id,
            project_root=self._project_root,
        )

        release = None
        if self._pre_authorise is not None:
            release = self._pre_authorise(context.tool_call_id)

        try:
            result = await self._service.execute(request, context)
        except asyncio.CancelledError:
            raise
        except ExecutionInfrastructureError as error:
            return HookOutcome(
                hook=hook,
                status="infrastructure_error",
                detail=str(error)[:MAX_DETAIL_CHARACTERS],
            )
        except Exception as error:  # noqa: BLE001 - a hook never breaks a turn
            return HookOutcome(
                hook=hook,
                status="failed",
                detail=str(error)[:MAX_DETAIL_CHARACTERS],
            )
        finally:
            if release is not None:
                release()

        detail = (
            result.reason_message
            or result.stderr
            or result.stdout
            or ""
        ).strip()
        return HookOutcome(
            hook=hook,
            status=result.status,
            exit_code=result.exit_code,
            detail=detail[:MAX_DETAIL_CHARACTERS],
        )
