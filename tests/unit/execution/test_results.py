from __future__ import annotations

import unittest
from datetime import UTC, datetime

from truecoder.execution.audit.models import (
    AuditRunPhase,
    AuditRunRecord,
    TerminalOutcome,
)
from truecoder.execution.backends.models import BackendExit, CleanupResult
from truecoder.execution.lifecycle import TerminalClaim
from truecoder.execution.models import NativeDiagnostic
from truecoder.execution.output import CollectedOutput, StreamOutput
from truecoder.execution.results import (
    TerminalMaterial,
    build_execution_result,
    build_finalization,
    build_output_evidence,
    claim_for_cancellation,
    claim_for_exit,
    claim_for_output_limit,
    claim_for_timeout,
    empty_output,
    public_status,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def material(
    claim: TerminalClaim,
    *,
    backend_exit: BackendExit | None = None,
    started: float | None = 10.0,
    finished: float = 12.5,
    cleanup: CleanupResult | None = None,
    output: CollectedOutput | None = None,
) -> TerminalMaterial:
    collected = output or empty_output()
    return TerminalMaterial(
        claim=claim,
        backend_exit=backend_exit,
        output=collected,
        audit_output=build_output_evidence(collected),
        cleanup=cleanup,
        started_at_monotonic=started,
        finished_at_monotonic=finished,
    )


def terminal_record(finalization) -> AuditRunRecord:
    return AuditRunRecord(
        run_id=finalization.run_id,
        created_at=NOW,
        updated_at=NOW,
        phase=AuditRunPhase.TERMINAL,
        finalization=finalization,
    )


def claim(status, reason, source, observed_at: float = 12.5) -> TerminalClaim:
    return TerminalClaim(
        status=status,
        reason=reason,
        observed_at_monotonic=observed_at,
        source=source,
    )


class StatusMappingTests(unittest.TestCase):
    def test_every_documented_route_maps_exactly(self):
        cases = (
            (
                claim("denied", None, "policy_denied"),
                None,
                None,
                "denied",
                None,
                None,
            ),
            (
                claim("failed_to_start", None, "failed_to_start"),
                None,
                None,
                "failed_to_start",
                None,
                None,
            ),
            (
                claim("completed", None, "backend_exit"),
                BackendExit(exit_code=0),
                10.0,
                "completed",
                0,
                None,
            ),
            (
                claim("failed", None, "backend_exit"),
                BackendExit(exit_code=7),
                10.0,
                "failed",
                7,
                None,
            ),
            (
                claim("timed_out", "timeout", "timeout"),
                BackendExit(exit_code=None, native_reason="timeout"),
                10.0,
                "timed_out",
                None,
                "timeout",
            ),
            (
                claim("cancelled", "cancellation", "cancellation"),
                BackendExit(exit_code=None, native_reason="cancellation"),
                10.0,
                "cancelled",
                None,
                "cancellation",
            ),
            (
                claim("cancelled", "shutdown", "cancellation"),
                BackendExit(exit_code=None, native_reason="shutdown"),
                10.0,
                "cancelled",
                None,
                "shutdown",
            ),
            (
                claim("limit_exceeded", "output_limit", "output_limit"),
                BackendExit(exit_code=None, native_reason="output_limit"),
                10.0,
                "limit_exceeded",
                None,
                "output_limit",
            ),
            (
                claim("limit_exceeded", "memory_limit", "resource_limit"),
                BackendExit(exit_code=None, native_reason="memory_limit"),
                10.0,
                "limit_exceeded",
                None,
                "memory_limit",
            ),
            (
                claim("limit_exceeded", "cpu_limit", "resource_limit"),
                BackendExit(exit_code=None, native_reason="cpu_limit"),
                10.0,
                "limit_exceeded",
                None,
                "cpu_limit",
            ),
            (
                claim("limit_exceeded", "process_limit", "resource_limit"),
                BackendExit(exit_code=None, native_reason="process_limit"),
                10.0,
                "limit_exceeded",
                None,
                "process_limit",
            ),
        )

        for (
            terminal_claim,
            backend_exit,
            started,
            status,
            exit_code,
            reason,
        ) in cases:
            with self.subTest(status=status, reason=reason):
                built = material(
                    terminal_claim,
                    backend_exit=backend_exit,
                    started=started,
                )
                finalization = build_finalization(
                    "run_01",
                    built,
                    finalized_at=NOW,
                )
                result = build_execution_result(
                    terminal_record(finalization),
                    built,
                    backend="posix",
                )

                self.assertEqual(result.status, status)
                self.assertEqual(result.exit_code, exit_code)
                self.assertEqual(result.termination_reason, reason)
                self.assertEqual(result.audit_id, "run_01")

    def test_denied_results_never_name_a_backend(self):
        built = material(claim("denied", None, "policy_denied"), started=None)
        finalization = build_finalization("run_02", built, finalized_at=NOW)

        result = build_execution_result(
            terminal_record(finalization),
            built,
            backend="posix",
        )

        self.assertIsNone(result.backend)

    def test_approval_rejection_uses_its_own_outcome(self):
        built = material(claim("denied", None, "approval_rejected"), started=None)

        finalization = build_finalization(
            "run_03",
            built,
            finalized_at=NOW,
            outcome_override=TerminalOutcome.APPROVAL_REJECTED,
        )

        self.assertIs(finalization.outcome, TerminalOutcome.APPROVAL_REJECTED)
        self.assertEqual(public_status(finalization), "denied")

    def test_duration_comes_from_monotonic_endpoints(self):
        built = material(
            claim("completed", None, "backend_exit"),
            backend_exit=BackendExit(exit_code=0),
            started=100.0,
            finished=103.25,
        )

        self.assertEqual(built.duration_seconds, 3.25)

    def test_a_backwards_monotonic_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            material(
                claim("completed", None, "backend_exit"),
                backend_exit=BackendExit(exit_code=0),
                started=100.0,
                finished=99.5,
            )

    def test_unstarted_runs_report_zero_duration(self):
        built = material(claim("denied", None, "policy_denied"), started=None)

        self.assertEqual(built.duration_seconds, 0.0)


class CleanupMappingTests(unittest.TestCase):
    def test_complete_cleanup_leaves_the_outcome_alone(self):
        built = material(
            claim("completed", None, "backend_exit"),
            backend_exit=BackendExit(exit_code=0),
            cleanup=CleanupResult(complete=True),
        )

        finalization = build_finalization("run_05", built, finalized_at=NOW)

        self.assertFalse(built.cleanup_incomplete)
        self.assertIs(finalization.outcome, TerminalOutcome.COMPLETED)
        self.assertIsNone(finalization.underlying_outcome)

    def test_cleanup_failure_is_never_reported_as_success(self):
        built = material(
            claim("completed", None, "backend_exit"),
            backend_exit=BackendExit(exit_code=0),
            cleanup=CleanupResult(
                complete=False,
                diagnostic=NativeDiagnostic(
                    code="cleanup-incomplete",
                    message="resource survived",
                    platform="posix",
                ),
            ),
        )

        finalization = build_finalization("run_04", built, finalized_at=NOW)

        self.assertIs(finalization.outcome, TerminalOutcome.CLEANUP_FAILED)
        self.assertIs(
            finalization.underlying_outcome,
            TerminalOutcome.COMPLETED,
        )
        self.assertTrue(built.cleanup_incomplete)
        self.assertEqual(public_status(finalization), "completed")


class OutputEvidenceTests(unittest.TestCase):
    def test_digests_come_from_raw_bytes_not_sanitized_text(self):
        collected = CollectedOutput(
            stdout=StreamOutput(
                text="clean",
                byte_count=12,
                sha256="a" * 64,
                truncated=False,
            ),
            stderr=StreamOutput(
                text="",
                byte_count=0,
                sha256=None,
                truncated=False,
            ),
            complete=True,
            output_limit_exceeded=False,
            retained_bytes=12,
        )

        evidence = build_output_evidence(collected)

        self.assertEqual(evidence.stdout_sha256, "a" * 64)
        self.assertEqual(evidence.stdout_bytes, 12)
        self.assertEqual(evidence.stdout_preview, "clean")

    def test_incomplete_output_is_marked_explicitly(self):
        evidence = build_output_evidence(empty_output(), complete=False)

        self.assertFalse(evidence.complete)

    def test_previews_are_bounded_and_flagged(self):
        text = "x" * 100
        collected = CollectedOutput(
            stdout=StreamOutput(
                text=text,
                byte_count=100,
                sha256="b" * 64,
                truncated=False,
            ),
            stderr=StreamOutput(text="", byte_count=0, sha256=None, truncated=False),
            complete=True,
            output_limit_exceeded=False,
            retained_bytes=100,
        )

        evidence = build_output_evidence(collected, preview_budget=10)

        self.assertEqual(evidence.stdout_preview, "x" * 10)
        self.assertTrue(evidence.stdout_truncated)


class ClaimFactoryTests(unittest.TestCase):
    def test_exit_claims_classify_limits_before_exit_codes(self):
        for reason in ("output_limit", "memory_limit", "cpu_limit", "process_limit"):
            with self.subTest(reason=reason):
                built = claim_for_exit(
                    BackendExit(exit_code=None, native_reason=reason),
                    1.0,
                )
                self.assertEqual(built.status, "limit_exceeded")
                self.assertEqual(built.reason, reason)
                self.assertEqual(built.source, "resource_limit")

    def test_shutdown_exit_is_distinguished_from_user_cancellation(self):
        shutdown = claim_for_exit(
            BackendExit(exit_code=None, native_reason="shutdown"),
            1.0,
        )
        cancelled = claim_for_exit(
            BackendExit(exit_code=None, native_reason="cancellation"),
            1.0,
        )

        self.assertEqual(shutdown.reason, "shutdown")
        self.assertEqual(cancelled.reason, "cancellation")

    def test_cancellation_reason_text_maps_to_the_domain_reason(self):
        self.assertEqual(claim_for_cancellation("shutdown", 1.0).reason, "shutdown")
        self.assertEqual(claim_for_cancellation("user", 1.0).reason, "cancellation")

    def test_timeout_and_output_limit_claims_are_fixed(self):
        self.assertEqual(claim_for_timeout(1.0).reason, "timeout")
        self.assertEqual(claim_for_output_limit(1.0).reason, "output_limit")


if __name__ == "__main__":
    unittest.main()
