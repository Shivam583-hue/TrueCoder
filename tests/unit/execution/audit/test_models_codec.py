from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta

from truecoder.execution.audit.codec import (
    AUDIT_CODEC_VERSION,
    deserialize_audit_model,
    serialize_audit_model,
)
from truecoder.execution.audit.models import (
    AuditEvent,
    AuditEventType,
    AuditFinalization,
    AuditRunAdmission,
    AuditRunHandle,
    AuditRunPhase,
    AuditRunRecord,
    AuditRunSnapshot,
    AuditRunStart,
    BackendResourceIdentifier,
    OutputEvidence,
    RecoveryResult,
    TerminalOutcome,
)
from truecoder.execution.errors import ExecutionSerializationError

NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def resource() -> BackendResourceIdentifier:
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="process_group",
        resource_id="4812",
        ownership_token="ownership-token",
        host_id="host-01",
        created_at_utc=NOW,
        native_details=(("pgid", "4812"),),
    )


def admission() -> AuditRunAdmission:
    return AuditRunAdmission(
        run_id="run-01",
        execution_id="exec-01",
        tool_call_id="call-01",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        request_sha256="a" * 64,
        request_summary=(("command", "python -V"),),
        created_at=NOW,
    )


def samples() -> tuple[object, ...]:
    native_resource = resource()
    output = OutputEvidence(
        stdout_sha256="b" * 64,
        stdout_bytes=7,
        stdout_preview="Python\n",
    )
    start = AuditRunStart(
        run_id="run-01",
        started_at=LATER,
        resource=native_resource,
        metadata=(("backend", "posix"),),
    )
    finalization = AuditFinalization(
        run_id="run-01",
        finalized_at=LATER + timedelta(seconds=1),
        outcome=TerminalOutcome.COMPLETED,
        command_started=True,
        exit_code=0,
        output=output,
        resource=native_resource,
    )
    record = AuditRunRecord(
        run_id="run-01",
        created_at=NOW,
        updated_at=LATER + timedelta(seconds=1),
        phase=AuditRunPhase.TERMINAL,
        start=start,
        finalization=finalization,
        revision=2,
    )
    return (
        admission(),
        AuditRunHandle(run_id="run-01", execution_id="exec-01"),
        native_resource,
        output,
        start,
        finalization,
        AuditEvent(
            event_id="event-01",
            run_id="run-01",
            sequence=2,
            occurred_at=LATER + timedelta(seconds=1),
            phase=AuditRunPhase.TERMINAL,
            event_type=AuditEventType.RUN_FINALIZED,
            terminal=True,
        ),
        record,
        AuditRunSnapshot(
            admission=admission(),
            record=record,
            resource=native_resource,
            recovery_owner="recovery-01",
            recovery_lease_until=LATER + timedelta(seconds=30),
        ),
    )


class AuditModelCodecTests(unittest.TestCase):
    def test_every_audit_model_is_immutable_and_round_trips(self):
        for model in samples():
            with self.subTest(model=type(model).__name__):
                self.assertTrue(is_dataclass(model))
                self.assertTrue(hasattr(type(model), "__slots__"))
                first = fields(model)[0]
                with self.assertRaises(FrozenInstanceError):
                    setattr(model, first.name, getattr(model, first.name))

                encoded = serialize_audit_model(model)  # type: ignore[arg-type]
                decoded = deserialize_audit_model(encoded)
                self.assertEqual(decoded, model)
                self.assertIs(type(decoded), type(model))

    def test_codec_is_deterministic_and_versioned(self):
        first = serialize_audit_model(admission())
        second = serialize_audit_model(admission())
        envelope = json.loads(first)

        self.assertEqual(first, second)
        self.assertEqual(envelope["version"], AUDIT_CODEC_VERSION)
        self.assertEqual(envelope["model"], "audit_run_admission")

    def test_codec_rejects_unknown_versions_and_fields(self):
        envelope = json.loads(serialize_audit_model(admission()))
        envelope["version"] = 999
        with self.assertRaises(ExecutionSerializationError):
            deserialize_audit_model(json.dumps(envelope))

        envelope["version"] = AUDIT_CODEC_VERSION
        envelope["unexpected"] = True
        with self.assertRaises(ExecutionSerializationError):
            deserialize_audit_model(json.dumps(envelope))

    def test_every_terminal_outcome_has_a_valid_finalization_shape(self):
        executed = {
            TerminalOutcome.COMPLETED: (0, None),
            TerminalOutcome.FAILED: (2, None),
            TerminalOutcome.TIMED_OUT: (None, None),
            TerminalOutcome.CANCELLED: (None, None),
            TerminalOutcome.LIMIT_EXCEEDED: (None, None),
            TerminalOutcome.CLEANUP_FAILED: (
                2,
                TerminalOutcome.FAILED,
            ),
        }
        pre_execution = {
            TerminalOutcome.POLICY_DENIED,
            TerminalOutcome.APPROVAL_REJECTED,
            TerminalOutcome.FAILED_TO_START,
        }
        recovery_outcomes = {
            TerminalOutcome.RECOVERED_NO_RESOURCE,
            TerminalOutcome.RECOVERED_RESOURCE_ABSENT,
            TerminalOutcome.RECOVERED_TERMINATED,
            TerminalOutcome.RECOVERY_FAILED,
        }
        covered = set(executed) | pre_execution | recovery_outcomes
        self.assertEqual(covered, set(TerminalOutcome))

        for outcome in pre_execution:
            with self.subTest(outcome=outcome):
                AuditFinalization(
                    run_id="run-01",
                    finalized_at=NOW,
                    outcome=outcome,
                    command_started=False,
                )

        for outcome, (exit_code, underlying) in executed.items():
            with self.subTest(outcome=outcome):
                AuditFinalization(
                    run_id="run-01",
                    finalized_at=NOW,
                    outcome=outcome,
                    command_started=True,
                    exit_code=exit_code,
                    underlying_outcome=underlying,
                )

        for outcome in recovery_outcomes:
            with self.subTest(outcome=outcome):
                native_resource = (
                    None
                    if outcome is TerminalOutcome.RECOVERED_NO_RESOURCE
                    else resource()
                )
                recovery = RecoveryResult(
                    run_id="run-01",
                    previous_phase=AuditRunPhase.PENDING,
                    attempted_at=NOW,
                    outcome=outcome,
                    resource=native_resource,
                )
                AuditFinalization(
                    run_id="run-01",
                    finalized_at=NOW,
                    outcome=outcome,
                    command_started=None,
                    resource=native_resource,
                    recovery=recovery,
                )


if __name__ == "__main__":
    unittest.main()
