from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

from truecoder.execution.models import (
    BACKEND_NAMES,
    BACKEND_PREFERENCES,
    CAPABILITY_LEVELS,
    EXECUTION_LIFECYCLE_STAGES,
    EXECUTION_MODES,
    EXECUTION_PLATFORMS,
    EXECUTION_STATUSES,
    FILESYSTEM_MODES,
    LIMIT_TERMINATION_REASONS,
    RESOLVED_SHELL_KINDS,
    SHELL_KINDS,
    TERMINATION_REASONS,
    BackendCapabilities,
    BackendName,
    BackendPreference,
    CapabilityCheck,
    CapabilityLevel,
    ExecutionContext,
    ExecutionLifecycleEvent,
    ExecutionLifecycleStage,
    ExecutionLimits,
    ExecutionMode,
    ExecutionPlatform,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    FilesystemMode,
    NativeDiagnostic,
    PolicyDecision,
    ResolvedShellKind,
    ShellKind,
    TerminationReason,
    normalize_environment_name,
)

UTC_NOW = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)
PROJECT_ROOT = Path.cwd().resolve()


def limits(**overrides: object) -> ExecutionLimits:
    values: dict[str, object] = {
        "timeout_seconds": 30.0,
        "max_output_bytes": 1024,
        "max_return_bytes": 512,
        "memory_bytes": 256 * 1024 * 1024,
        "cpu_seconds": 10.0,
        "max_processes": 32,
        "termination_grace_seconds": 2.0,
    }
    values.update(overrides)
    return ExecutionLimits(**values)  # type: ignore[arg-type]


def exec_request(**overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "mode": "exec",
        "argv": ("pytest", "-q"),
        "script": None,
        "working_directory": PROJECT_ROOT,
        "limits": limits(),
        "network_access": False,
        "filesystem_mode": "workspace-read",
        "backend": "auto",
        "shell_kind": "auto",
        "environment": (("CI", "1"),),
        "require_cancellation": True,
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def result(**overrides: object) -> ExecutionResult:
    values: dict[str, object] = {
        "status": "completed",
        "exit_code": 0,
        "stdout": "ok\n",
        "stderr": "",
        "duration_seconds": 0.25,
        "stdout_bytes": 3,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "termination_reason": None,
        "backend": "posix",
        "audit_id": "exec_01",
    }
    values.update(overrides)
    return ExecutionResult(**values)  # type: ignore[arg-type]


class DomainVocabularyTests(unittest.TestCase):
    def test_runtime_vocabulary_matches_literal_types(self):
        vocabulary_pairs = (
            (EXECUTION_STATUSES, ExecutionStatus),
            (EXECUTION_LIFECYCLE_STAGES, ExecutionLifecycleStage),
            (EXECUTION_MODES, ExecutionMode),
            (BACKEND_PREFERENCES, BackendPreference),
            (BACKEND_NAMES, BackendName),
            (EXECUTION_PLATFORMS, ExecutionPlatform),
            (SHELL_KINDS, ShellKind),
            (RESOLVED_SHELL_KINDS, ResolvedShellKind),
            (FILESYSTEM_MODES, FilesystemMode),
            (CAPABILITY_LEVELS, CapabilityLevel),
            (TERMINATION_REASONS, TerminationReason),
        )

        for runtime_values, literal_type in vocabulary_pairs:
            with self.subTest(literal_type=literal_type):
                self.assertEqual(runtime_values, frozenset(get_args(literal_type)))

        self.assertLessEqual(LIMIT_TERMINATION_REASONS, TERMINATION_REASONS)

    def test_environment_name_normalization_is_platform_aware(self):
        self.assertEqual(normalize_environment_name("Path", "posix"), "Path")
        self.assertEqual(normalize_environment_name("Path", "windows"), "path")
        self.assertNotEqual(
            normalize_environment_name("PATH", "posix"),
            normalize_environment_name("Path", "posix"),
        )
        self.assertEqual(
            normalize_environment_name("PATH", "windows"),
            normalize_environment_name("Path", "windows"),
        )

        with self.assertRaises(ValueError):
            normalize_environment_name("", "posix")
        with self.assertRaises(ValueError):
            normalize_environment_name("PATH", "unknown")  # type: ignore[arg-type]


class ExecutionLimitsTests(unittest.TestCase):
    def test_accepts_finite_bounded_values(self):
        value = limits()

        self.assertEqual(value.timeout_seconds, 30.0)
        self.assertEqual(value.max_return_bytes, 512)
        self.assertEqual(value.termination_grace_seconds, 2.0)

    def test_rejects_invalid_limits(self):
        invalid_values = (
            ("timeout_seconds", 0),
            ("timeout_seconds", math.inf),
            ("timeout_seconds", math.nan),
            ("timeout_seconds", True),
            ("max_output_bytes", 0),
            ("max_output_bytes", 1.5),
            ("max_return_bytes", -1),
            ("max_return_bytes", True),
            ("memory_bytes", 0),
            ("cpu_seconds", 0),
            ("cpu_seconds", math.inf),
            ("max_processes", 0),
            ("termination_grace_seconds", -0.1),
        )

        for field_name, field_value in invalid_values:
            with (
                self.subTest(field=field_name, value=field_value),
                self.assertRaises((TypeError, ValueError)),
            ):
                limits(**{field_name: field_value})

    def test_return_limit_cannot_exceed_produced_output_limit(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            limits(max_output_bytes=100, max_return_bytes=101)


class ExecutionRequestTests(unittest.TestCase):
    def test_exec_request_is_immutable_and_canonicalizes_host_path(self):
        request = exec_request(
            working_directory=PROJECT_ROOT / "src" / "..",
        )

        self.assertEqual(request.working_directory, PROJECT_ROOT)
        self.assertIsInstance(request.argv, tuple)
        with self.assertRaises(FrozenInstanceError):
            request.mode = "shell"  # type: ignore[misc]

    def test_exec_and_shell_inputs_are_mutually_exclusive(self):
        invalid_exec_requests = (
            {"argv": None},
            {"argv": ()},
            {"argv": ("",)},
            {"argv": ("   ",)},
            {"argv": ("py\x00test",)},
            {"script": "pytest"},
            {"shell_kind": "posix"},
            {"mode": "unknown"},
        )

        for overrides in invalid_exec_requests:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                exec_request(**overrides)

        shell_values: dict[str, object] = {
            "mode": "shell",
            "argv": None,
            "script": "pytest -q && ruff check .",
            "shell_kind": "posix",
        }
        shell_request = exec_request(**shell_values)
        self.assertEqual(shell_request.script, shell_values["script"])

        invalid_shell_requests = (
            {"argv": ("pytest",)},
            {"script": None},
            {"script": ""},
            {"script": "   "},
            {"script": "echo\x00bad"},
        )
        for overrides in invalid_shell_requests:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                exec_request(**{**shell_values, **overrides})

    def test_requires_an_absolute_host_path(self):
        with self.assertRaisesRegex(ValueError, "absolute host path"):
            exec_request(working_directory=Path("relative"))

        with self.assertRaises(TypeError):
            exec_request(working_directory=str(PROJECT_ROOT))

    def test_validates_environment_pairs_and_powershell_casing(self):
        invalid_environments = (
            [("CI", "1")],
            (("CI",),),
            (("", "1"),),
            (("   ", "1"),),
            (("A=B", "1"),),
            (("A\x00B", "1"),),
            (("A", "1\x00"),),
            (("A", "1"), ("A", "2")),
        )
        for environment in invalid_environments:
            with (
                self.subTest(environment=environment),
                self.assertRaises((TypeError, ValueError)),
            ):
                exec_request(environment=environment)

        with self.assertRaisesRegex(ValueError, "duplicate"):
            exec_request(
                mode="shell",
                argv=None,
                script="$env:Path",
                shell_kind="powershell",
                environment=(("PATH", "one"), ("Path", "two")),
            )

        request = exec_request(
            mode="shell",
            argv=None,
            script="printf '%s\\n' \"$PATH\" \"$Path\"",
            shell_kind="posix",
            environment=(("PATH", "one"), ("Path", "two")),
        )
        self.assertEqual(len(request.environment), 2)

    def test_validates_boolean_and_nested_model_fields(self):
        for field_name in ("network_access", "require_cancellation"):
            with self.subTest(field=field_name), self.assertRaises(TypeError):
                exec_request(**{field_name: 1})

        with self.assertRaises(TypeError):
            exec_request(limits={})  # type: ignore[arg-type]


class DecisionAndCapabilityTests(unittest.TestCase):
    def test_policy_decision_requires_a_reason_when_denied(self):
        allowed = PolicyDecision(
            allowed=True,
            reason=None,
            effective_limits=limits(),
        )
        denied = PolicyDecision(
            allowed=False,
            reason="network access is forbidden",
            effective_limits=limits(),
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)
        with self.assertRaisesRegex(ValueError, "must include a reason"):
            PolicyDecision(
                allowed=False,
                reason=None,
                effective_limits=limits(),
            )

    def test_backend_capabilities_validate_modes_and_shells(self):
        capabilities = BackendCapabilities(
            filesystem_isolation="enforced",
            network_isolation="enforced",
            memory_limits="enforced",
            cpu_limits="best_effort",
            process_limits="enforced",
            timeout_enforcement="enforced",
            cancellation="enforced",
            supported_execution_modes=("exec", "shell"),
            supported_filesystem_modes=("workspace-read", "workspace-write"),
            supported_shells=("posix",),
        )

        self.assertIn("shell", capabilities.supported_execution_modes)
        with self.assertRaisesRegex(ValueError, "at least one shell"):
            replace(capabilities, supported_shells=())
        with self.assertRaisesRegex(ValueError, "exec-only"):
            replace(
                capabilities,
                supported_execution_modes=("exec",),
                supported_shells=("posix",),
            )
        with self.assertRaisesRegex(ValueError, "duplicates"):
            replace(
                capabilities,
                supported_execution_modes=("exec", "exec"),
                supported_shells=(),
            )
        with self.assertRaises(ValueError):
            replace(capabilities, memory_limits="sometimes")  # type: ignore[arg-type]

    def test_capability_check_has_consistent_reason_semantics(self):
        self.assertTrue(CapabilityCheck(compatible=True).compatible)
        self.assertFalse(
            CapabilityCheck(
                compatible=False,
                reasons=("network isolation unavailable",),
            ).compatible
        )

        invalid = (
            {"compatible": True, "reasons": ("unexpected",)},
            {"compatible": False, "reasons": ()},
            {"compatible": False, "reasons": ("same", "same")},
            {"compatible": False, "reasons": ("",)},
        )
        for values in invalid:
            with (
                self.subTest(values=values),
                self.assertRaises((TypeError, ValueError)),
            ):
                CapabilityCheck(**values)  # type: ignore[arg-type]


class ExecutionResultTests(unittest.TestCase):
    def test_every_status_has_a_valid_normalized_result(self):
        results = (
            result(status="completed", exit_code=0),
            result(status="failed", exit_code=1),
            result(
                status="timed_out",
                exit_code=None,
                termination_reason="timeout",
            ),
            result(
                status="cancelled",
                exit_code=None,
                termination_reason="cancellation",
            ),
            result(
                status="denied",
                exit_code=None,
                backend=None,
                termination_reason=None,
            ),
            result(
                status="limit_exceeded",
                exit_code=None,
                termination_reason="output_limit",
            ),
            result(
                status="failed_to_start",
                exit_code=None,
                termination_reason=None,
            ),
        )

        self.assertEqual({item.status for item in results}, EXECUTION_STATUSES)

    def test_each_limit_reason_is_valid_for_limit_exceeded(self):
        for reason in LIMIT_TERMINATION_REASONS:
            with self.subTest(reason=reason):
                value = result(
                    status="limit_exceeded",
                    exit_code=None,
                    termination_reason=reason,
                )
                self.assertEqual(value.termination_reason, reason)

    def test_result_status_invariants_are_exhaustive(self):
        invalid_results = (
            {"status": "completed", "exit_code": 1},
            {"status": "completed", "backend": None},
            {"status": "completed", "termination_reason": "timeout"},
            {"status": "failed", "exit_code": None},
            {"status": "failed", "exit_code": 0},
            {"status": "failed", "exit_code": 1, "backend": None},
            {"status": "failed", "exit_code": 1, "termination_reason": "timeout"},
            {
                "status": "timed_out",
                "exit_code": None,
                "termination_reason": None,
            },
            {
                "status": "timed_out",
                "exit_code": None,
                "termination_reason": "cancellation",
            },
            {
                "status": "cancelled",
                "exit_code": None,
                "termination_reason": "timeout",
            },
            {
                "status": "limit_exceeded",
                "exit_code": None,
                "termination_reason": "timeout",
            },
            {"status": "denied", "exit_code": 1, "backend": None},
            {"status": "denied", "exit_code": None, "backend": "posix"},
            {
                "status": "denied",
                "exit_code": None,
                "backend": None,
                "termination_reason": "cancellation",
            },
            {"status": "failed_to_start", "exit_code": 1},
            {
                "status": "failed_to_start",
                "exit_code": None,
                "termination_reason": "timeout",
            },
            {"status": "unknown"},
        )

        for overrides in invalid_results:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                result(**overrides)

    def test_nonzero_exit_is_data_not_an_infrastructure_exception(self):
        value = result(
            status="failed",
            exit_code=2,
            stderr="tests failed",
            stderr_bytes=12,
        )

        self.assertEqual(value.status, "failed")
        self.assertEqual(value.exit_code, 2)
        self.assertEqual(value.stderr, "tests failed")

    def test_result_validates_metrics_and_identifiers(self):
        invalid_results = (
            {"duration_seconds": -1},
            {"duration_seconds": math.inf},
            {"stdout_bytes": -1},
            {"stderr_bytes": True},
            {"stdout_truncated": 1},
            {"audit_id": ""},
            {"backend": "unknown"},
            {"exit_code": True},
        )

        for overrides in invalid_results:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                result(**overrides)


class ContextDiagnosticAndEventTests(unittest.TestCase):
    def test_context_requires_utc_and_canonical_project_root(self):
        context = ExecutionContext(
            execution_id="exec_01",
            tool_call_id="call_01",
            session_id="session_01",
            turn_id="turn_01",
            workspace_id="workspace_01",
            project_root=PROJECT_ROOT / "src" / "..",
            launched_at_utc=UTC_NOW,
        )

        self.assertEqual(context.project_root, PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            replace(context, launched_at_utc=UTC_NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "expressed in UTC"):
            replace(
                context,
                launched_at_utc=UTC_NOW.astimezone(
                    timezone(timedelta(hours=1))
                ),
            )

    def test_native_diagnostic_preserves_platform_specific_data(self):
        diagnostics = (
            NativeDiagnostic(
                code="SIGKILL",
                message="terminated by signal",
                platform="linux",
            ),
            NativeDiagnostic(
                code=5,
                message="access denied",
                platform="windows",
            ),
            NativeDiagnostic(
                code=None,
                message="runtime did not report a code",
                platform="container",
            ),
        )

        self.assertEqual(
            [diagnostic.platform for diagnostic in diagnostics],
            ["linux", "windows", "container"],
        )
        with self.assertRaises(TypeError):
            NativeDiagnostic(code=True, message="bad", platform="windows")
        with self.assertRaises(ValueError):
            NativeDiagnostic(code=None, message="", platform="windows")

    def test_every_lifecycle_stage_can_be_represented(self):
        events = tuple(
            ExecutionLifecycleEvent(
                execution_id="exec_01",
                stage=stage,
                occurred_at_utc=UTC_NOW,
                sequence=index,
                message=f"entered {stage}",
                details=(("backend", "posix"),),
            )
            for index, stage in enumerate(sorted(EXECUTION_LIFECYCLE_STAGES))
        )

        self.assertEqual({event.stage for event in events}, EXECUTION_LIFECYCLE_STAGES)
        self.assertTrue(all(isinstance(event.details, tuple) for event in events))

    def test_lifecycle_event_rejects_invalid_metadata(self):
        base = ExecutionLifecycleEvent(
            execution_id="exec_01",
            stage="started",
            occurred_at_utc=UTC_NOW,
            sequence=1,
        )
        invalid = (
            {"execution_id": ""},
            {"stage": "unknown"},
            {"occurred_at_utc": UTC_NOW.replace(tzinfo=None)},
            {"sequence": -1},
            {"message": ""},
            {"details": (("key", "one"), ("key", "two"))},
        )

        for overrides in invalid:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((TypeError, ValueError)),
            ):
                replace(base, **overrides)


if __name__ == "__main__":
    unittest.main()
