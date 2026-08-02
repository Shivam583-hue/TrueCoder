from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Protocol

from .models import (
    MAX_AUDIT_IDENTIFIER_CHARS,
    AuditEventType,
    AuditFinalization,
    AuditRunHandle,
    AuditRunRecord,
    AuditRunSnapshot,
    BackendResourceIdentifier,
    RecoveryResult,
    TerminalOutcome,
)
from .service import AuditService


class RecoveryDisposition(str, Enum):
    RESOURCE_ABSENT = "resource_absent"
    TERMINATED = "terminated"


class RecoveryHandler(Protocol):
    """Recover one exact native resource; never rediscover it heuristically."""

    async def recover(
        self,
        resource: BackendResourceIdentifier,
    ) -> RecoveryDisposition: ...


class AuditRecoveryCoordinator:
    def __init__(
        self,
        audit: AuditService,
        handlers: Mapping[str, RecoveryHandler],
    ) -> None:
        self._audit = audit
        self._handlers = dict(handlers)

    async def recover_startup(
        self,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        limit: int = 100,
    ) -> tuple[AuditRunRecord, ...]:
        snapshots = await self._audit.claim_nonterminal(
            owner,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        recovered: list[AuditRunRecord] = []
        for snapshot in snapshots:
            recovered.append(await self._recover_one(snapshot))
        return tuple(recovered)

    async def _recover_one(self, snapshot: AuditRunSnapshot) -> AuditRunRecord:
        run_id = snapshot.record.run_id
        attempted_at = self._audit.now()
        await self._audit.append_event(
            _handle(snapshot),
            AuditEventType.RECOVERY_STARTED,
            metadata=(("previous_phase", snapshot.record.phase.value),),
        )

        if snapshot.resource is None:
            return await self._finalize_recovery(
                snapshot,
                attempted_at=attempted_at,
                outcome=TerminalOutcome.RECOVERED_NO_RESOURCE,
                detail="no backend resource was durably attached",
            )

        handler = self._handlers.get(snapshot.resource.backend)
        if handler is None:
            return await self._finalize_recovery(
                snapshot,
                attempted_at=attempted_at,
                outcome=TerminalOutcome.RECOVERY_FAILED,
                detail=f"no recovery handler for backend {snapshot.resource.backend}",
            )

        try:
            disposition = await handler.recover(snapshot.resource)
        except Exception as error:  # noqa: BLE001 - failures must become evidence
            return await self._finalize_recovery(
                snapshot,
                attempted_at=attempted_at,
                outcome=TerminalOutcome.RECOVERY_FAILED,
                detail=_bounded_detail(
                    f"recovery handler failed: {type(error).__name__}: {error}"
                ),
            )

        if disposition is RecoveryDisposition.RESOURCE_ABSENT:
            outcome = TerminalOutcome.RECOVERED_RESOURCE_ABSENT
            event_type = AuditEventType.RECOVERY_RESOURCE_ABSENT
        elif disposition is RecoveryDisposition.TERMINATED:
            outcome = TerminalOutcome.RECOVERED_TERMINATED
            event_type = AuditEventType.RECOVERY_TERMINATED
        else:
            return await self._finalize_recovery(
                snapshot,
                attempted_at=attempted_at,
                outcome=TerminalOutcome.RECOVERY_FAILED,
                detail=_bounded_detail(
                    f"recovery handler returned an invalid disposition for {run_id}"
                ),
            )
        await self._audit.append_event(
            _handle(snapshot),
            event_type,
            metadata=(("backend", snapshot.resource.backend),),
        )
        return await self._finalize_recovery(
            snapshot,
            attempted_at=attempted_at,
            outcome=outcome,
        )

    async def _finalize_recovery(
        self,
        snapshot: AuditRunSnapshot,
        *,
        attempted_at: datetime,
        outcome: TerminalOutcome,
        detail: str | None = None,
    ) -> AuditRunRecord:
        if outcome is TerminalOutcome.RECOVERY_FAILED:
            await self._audit.append_event(
                _handle(snapshot),
                AuditEventType.RECOVERY_FAILED,
                message=detail,
            )
        result = RecoveryResult(
            run_id=snapshot.record.run_id,
            previous_phase=snapshot.record.phase,
            attempted_at=attempted_at,
            outcome=outcome,
            resource=snapshot.resource,
            detail=detail,
        )
        finalization = AuditFinalization(
            run_id=snapshot.record.run_id,
            finalized_at=self._audit.now(),
            outcome=outcome,
            command_started=None,
            resource=snapshot.resource,
            recovery=result,
            detail=detail,
        )
        return await self._audit.finalize(_handle(snapshot), finalization)


def _handle(snapshot: AuditRunSnapshot) -> AuditRunHandle:
    return AuditRunHandle(
        run_id=snapshot.admission.run_id,
        execution_id=snapshot.admission.execution_id,
    )


def _bounded_detail(value: str) -> str:
    if len(value) <= MAX_AUDIT_IDENTIFIER_CHARS:
        return value
    marker = "...[truncated]"
    return value[: MAX_AUDIT_IDENTIFIER_CHARS - len(marker)] + marker
