from __future__ import annotations

import json
import os
import unittest
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from truecoder.execution.errors import ExecutionSerializationError
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityCheck,
    CapabilityRequirements,
    ExecutionContext,
    ExecutionLifecycleEvent,
    ExecutionLimits,
    ExecutionRequest,
    ExecutionResult,
    NativeDiagnostic,
    PolicyDecision,
    PolicyReason,
    RiskLevel,
)
from truecoder.execution.serialization import (
    SERIALIZATION_VERSION,
    deserialize_execution_model,
    serialize_execution_model,
)

UTC_NOW = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)
PROJECT_ROOT = Path.cwd().resolve()


def limits() -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=30.0,
        max_output_bytes=4096,
        max_return_bytes=1024,
        memory_bytes=512 * 1024 * 1024,
        cpu_seconds=15.5,
        max_processes=64,
        termination_grace_seconds=2.5,
    )


def sample_models() -> tuple[object, ...]:
    shared_limits = limits()
    policy_reason = PolicyReason(
        code="network-denied",
        message="Network access is disabled.",
        rule_id="policy.network.001",
    )
    requirements = CapabilityRequirements(
        filesystem_isolation="enforced",
        network_isolation="enforced",
        memory_limits="enforced",
        timeout_enforcement="enforced",
        cancellation="enforced",
    )
    return (
        shared_limits,
        ExecutionRequest(
            mode="exec",
            argv=("python", "-c", "print('héllo')"),
            script=None,
            working_directory=PROJECT_ROOT,
            limits=shared_limits,
            network_access=False,
            filesystem_mode="workspace-read",
            backend="container",
            shell_kind="auto",
            environment=(("CI", "1"), ("LANG", "C.UTF-8")),
            require_cancellation=True,
        ),
        policy_reason,
        requirements,
        PolicyDecision(
            allowed=False,
            risk=RiskLevel.CRITICAL,
            requires_approval=False,
            effective_limits=shared_limits,
            requirements=requirements,
            reasons=(policy_reason,),
        ),
        BackendCapabilities(
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
        ),
        CapabilityCheck(
            compatible=False,
            reasons=(
                "network isolation cannot be enforced",
                "memory limit cannot be enforced",
            ),
        ),
        ExecutionResult(
            status="failed",
            exit_code=2,
            stdout="",
            stderr="two tests failed\n",
            duration_seconds=1.75,
            stdout_bytes=0,
            stderr_bytes=17,
            stdout_truncated=False,
            stderr_truncated=False,
            termination_reason=None,
            backend="posix",
            audit_id="exec_01",
        ),
        ExecutionContext(
            execution_id="exec_01",
            tool_call_id="call_01",
            session_id="session_01",
            turn_id="turn_01",
            workspace_id="workspace_01",
            project_root=PROJECT_ROOT,
            launched_at_utc=UTC_NOW,
        ),
        NativeDiagnostic(
            code="SIGKILL",
            message="process group was force-killed",
            platform="linux",
        ),
        ExecutionLifecycleEvent(
            execution_id="exec_01",
            stage="limit_exceeded",
            occurred_at_utc=UTC_NOW,
            sequence=7,
            message="output limit reached",
            details=(("limit", "output"), ("backend", "posix")),
        ),
    )


def sample_model(model_type: type[object]) -> object:
    return next(model for model in sample_models() if isinstance(model, model_type))


class ExecutionSerializationTests(unittest.TestCase):
    def test_every_shared_domain_model_is_frozen_and_slotted(self):
        for model in sample_models():
            with self.subTest(model=type(model).__name__):
                self.assertTrue(is_dataclass(model))
                self.assertTrue(hasattr(type(model), "__slots__"))
                first_field = fields(model)[0]
                with self.assertRaises(FrozenInstanceError):
                    setattr(model, first_field.name, getattr(model, first_field.name))

    def test_round_trips_every_shared_domain_model(self):
        for model in sample_models():
            with self.subTest(model=type(model).__name__):
                payload = serialize_execution_model(model)  # type: ignore[arg-type]
                restored = deserialize_execution_model(payload)

                self.assertEqual(restored, model)
                self.assertIs(type(restored), type(model))

    def test_serialization_is_deterministic_versioned_and_unicode_safe(self):
        request = sample_model(ExecutionRequest)

        first = serialize_execution_model(request)  # type: ignore[arg-type]
        second = serialize_execution_model(request)  # type: ignore[arg-type]
        envelope = json.loads(first)

        self.assertEqual(first, second)
        self.assertEqual(envelope["version"], SERIALIZATION_VERSION)
        self.assertEqual(envelope["model"], "execution_request")
        self.assertIn("héllo", first)
        self.assertNotIn("\\u00e9", first)

    def test_restores_immutable_collection_and_host_types(self):
        request = deserialize_execution_model(
            serialize_execution_model(  # type: ignore[arg-type]
                sample_model(ExecutionRequest)
            )
        )
        context = deserialize_execution_model(
            serialize_execution_model(  # type: ignore[arg-type]
                sample_model(ExecutionContext)
            )
        )

        self.assertIsInstance(request, ExecutionRequest)
        self.assertIsInstance(request.argv, tuple)
        self.assertIsInstance(request.environment, tuple)
        self.assertIsInstance(context, ExecutionContext)
        self.assertIsInstance(context.project_root, Path)
        self.assertEqual(context.launched_at_utc.tzinfo, timezone.utc)

    def test_rejects_non_string_empty_and_invalid_json_payloads(self):
        with self.assertRaises(TypeError):
            deserialize_execution_model({})  # type: ignore[arg-type]

        for payload in ("", "   ", "not-json", "[]", "null"):
            with (
                self.subTest(payload=payload),
                self.assertRaises(ExecutionSerializationError),
            ):
                deserialize_execution_model(payload)

    def test_rejects_unknown_versions_models_and_envelope_fields(self):
        valid = json.loads(serialize_execution_model(limits()))
        invalid_envelopes = (
            {**valid, "version": SERIALIZATION_VERSION + 1},
            {**valid, "version": True},
            {**valid, "model": "unknown"},
            {key: value for key, value in valid.items() if key != "data"},
            {**valid, "unexpected": True},
        )

        for envelope in invalid_envelopes:
            with (
                self.subTest(envelope=envelope),
                self.assertRaises(ExecutionSerializationError),
            ):
                deserialize_execution_model(json.dumps(envelope))

    def test_rejects_missing_unknown_and_invalid_model_fields(self):
        valid = json.loads(
            serialize_execution_model(
                ExecutionResult(
                    status="completed",
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    duration_seconds=0.1,
                    stdout_bytes=2,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    termination_reason=None,
                    backend="posix",
                    audit_id="exec_01",
                )
            )
        )
        missing = json.loads(json.dumps(valid))
        del missing["data"]["status"]
        unknown = json.loads(json.dumps(valid))
        unknown["data"]["extra"] = "value"
        inconsistent = json.loads(json.dumps(valid))
        inconsistent["data"]["status"] = "failed"
        inconsistent["data"]["exit_code"] = 0

        for envelope in (missing, unknown, inconsistent):
            with (
                self.subTest(envelope=envelope),
                self.assertRaises(ExecutionSerializationError),
            ):
                deserialize_execution_model(json.dumps(envelope))

    def test_host_paths_carry_an_explicit_platform_flavor(self):
        request = ExecutionRequest(
            mode="exec",
            argv=("pytest",),
            script=None,
            working_directory=PROJECT_ROOT,
            limits=limits(),
            network_access=False,
            filesystem_mode="workspace-read",
        )
        envelope = json.loads(serialize_execution_model(request))
        path_data = envelope["data"]["working_directory"]
        expected_flavor = "windows" if os.name == "nt" else "posix"

        self.assertEqual(path_data["flavor"], expected_flavor)

        path_data["flavor"] = "posix" if expected_flavor == "windows" else "windows"
        with self.assertRaisesRegex(
            ExecutionSerializationError,
            "cannot be restored",
        ):
            deserialize_execution_model(json.dumps(envelope))

    def test_rejects_malformed_nested_collections(self):
        request = sample_model(ExecutionRequest)
        envelope = json.loads(
            serialize_execution_model(request)  # type: ignore[arg-type]
        )
        invalid_values = (
            {"argv": "pytest"},
            {"environment": [["CI"]]},
            {"environment": {"CI": "1"}},
            {"limits": []},
            {"working_directory": str(PROJECT_ROOT)},
        )

        for changes in invalid_values:
            malformed = json.loads(json.dumps(envelope))
            malformed["data"].update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(ExecutionSerializationError),
            ):
                deserialize_execution_model(json.dumps(malformed))

    def test_rejects_malformed_structured_policy_fields(self):
        decision = sample_model(PolicyDecision)
        envelope = json.loads(
            serialize_execution_model(decision)  # type: ignore[arg-type]
        )
        invalid_values = (
            {"reasons": "network denied"},
            {"reasons": [{"code": "missing-fields"}]},
            {"requirements": []},
            {"risk": "unknown"},
            {"requires_approval": "yes"},
        )

        for changes in invalid_values:
            malformed = json.loads(json.dumps(envelope))
            malformed["data"].update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(ExecutionSerializationError),
            ):
                deserialize_execution_model(json.dumps(malformed))

    def test_serializer_rejects_unknown_objects(self):
        with self.assertRaises(TypeError):
            serialize_execution_model(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
