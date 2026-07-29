from __future__ import annotations

import unittest

from truecoder.execution.errors import (
    ApprovalError,
    AuditError,
    AuditPersistenceError,
    BackendCleanupError,
    BackendDiscoveryError,
    BackendError,
    BackendOperationError,
    BackendSelectionError,
    BackendStartError,
    BackendTerminationError,
    BackendUnavailableError,
    EnvironmentConstructionError,
    ExecutionInfrastructureError,
    ExecutionSerializationError,
    InvalidExecutionStateError,
    NoCompatibleBackendError,
    OutputCollectionError,
    PolicyEvaluationError,
    RequestValidationError,
)
from truecoder.execution.models import NativeDiagnostic


class ExecutionInfrastructureErrorTests(unittest.TestCase):
    def test_preserves_structured_cross_platform_metadata(self):
        diagnostic = NativeDiagnostic(
            code=5,
            message="access denied",
            platform="windows",
        )
        error = BackendStartError(
            "could not create process",
            execution_id="exec_01",
            backend="windows",
            operation="start",
            diagnostic=diagnostic,
        )

        self.assertEqual(str(error), "could not create process")
        self.assertEqual(error.message, "could not create process")
        self.assertEqual(error.execution_id, "exec_01")
        self.assertEqual(error.backend, "windows")
        self.assertEqual(error.operation, "start")
        self.assertIs(error.diagnostic, diagnostic)

    def test_rejects_invalid_base_metadata(self):
        invalid_arguments = (
            {"message": ""},
            {"message": 1},
            {"message": "failed", "execution_id": ""},
            {"message": "failed", "backend": "unknown"},
            {"message": "failed", "operation": ""},
            {"message": "failed", "diagnostic": "not diagnostic"},
        )

        for arguments in invalid_arguments:
            with (
                self.subTest(arguments=arguments),
                self.assertRaises((TypeError, ValueError)),
            ):
                ExecutionInfrastructureError(**arguments)  # type: ignore[arg-type]

    def test_error_hierarchy_separates_infrastructure_categories(self):
        backend_errors = (
            BackendDiscoveryError,
            BackendSelectionError,
            BackendStartError,
            BackendOperationError,
            BackendTerminationError,
            BackendCleanupError,
        )
        service_errors = (
            OutputCollectionError,
            RequestValidationError,
            PolicyEvaluationError,
            ApprovalError,
            EnvironmentConstructionError,
            ExecutionSerializationError,
            AuditPersistenceError,
            InvalidExecutionStateError,
        )

        for error_type in backend_errors:
            with self.subTest(error_type=error_type):
                self.assertTrue(issubclass(error_type, BackendError))
                self.assertTrue(
                    issubclass(error_type, ExecutionInfrastructureError)
                )

        self.assertTrue(issubclass(BackendTerminationError, BackendOperationError))
        self.assertTrue(issubclass(BackendCleanupError, BackendOperationError))
        self.assertTrue(issubclass(AuditPersistenceError, AuditError))

        for error_type in service_errors:
            with self.subTest(error_type=error_type):
                self.assertTrue(
                    issubclass(error_type, ExecutionInfrastructureError)
                )

    def test_backend_unavailable_requires_a_specific_valid_preference(self):
        error = BackendUnavailableError(
            "container runtime is unavailable",
            preference="container",
            execution_id="exec_01",
        )

        self.assertEqual(error.preference, "container")
        self.assertEqual(error.operation, "select_backend")

        for preference in ("auto", "unknown"):
            with (
                self.subTest(preference=preference),
                self.assertRaises(ValueError),
            ):
                BackendUnavailableError(
                    "unavailable",
                    preference=preference,  # type: ignore[arg-type]
                )

    def test_no_compatible_backend_validates_structured_failures(self):
        failures = (
            ("posix", ("network isolation cannot be enforced",)),
            ("container", ("container runtime is unavailable",)),
        )
        error = NoCompatibleBackendError(
            failures=failures,
            preference="auto",
            execution_id="exec_01",
        )

        self.assertEqual(error.failures, failures)
        self.assertEqual(error.preference, "auto")
        self.assertEqual(error.operation, "select_backend")

        invalid_failures = (
            [("posix", ("reason",))],
            (("unknown", ("reason",)),),
            (("posix", ()),),
            (("posix", ("",)),),
            (("posix", ("same", "same")),),
            (
                ("posix", ("one",)),
                ("posix", ("two",)),
            ),
        )
        for value in invalid_failures:
            with (
                self.subTest(failures=value),
                self.assertRaises((TypeError, ValueError)),
            ):
                NoCompatibleBackendError(
                    failures=value,  # type: ignore[arg-type]
                )

        with self.assertRaises(ValueError):
            NoCompatibleBackendError(preference="unknown")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
