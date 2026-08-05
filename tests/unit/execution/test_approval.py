from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.approval import (
    ApprovalDecision,
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalScope,
    ApprovalService,
    ExecutionApprovalDetails,
    ExecutionApprovalGate,
    RiskLevel,
)
from truecoder.execution.backends.models import (
    BackendDescriptor,
    ContainerRuntimeInfo,
)
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import ApprovalError
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityRequirements,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    PolicyDecision,
    PolicyReason,
)
from truecoder.execution.preparation import PreparedExecution

ROOT = Path.cwd().resolve()


def limits(timeout_seconds: float = 30) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_output_bytes=128_000,
        max_return_bytes=32_000,
        memory_bytes=512_000_000,
        cpu_seconds=20,
        max_processes=32,
    )


def capabilities(
    *,
    network_isolation: str = "enforced",
) -> BackendCapabilities:
    return BackendCapabilities(
        filesystem_isolation="enforced",
        network_isolation=network_isolation,  # type: ignore[arg-type]
        memory_limits="enforced",
        cpu_limits="enforced",
        process_limits="enforced",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=("exec", "shell"),
        supported_filesystem_modes=("workspace-read", "workspace-write"),
        supported_shells=("posix",),
    )


def execution_details(
    *,
    mode: str = "exec",
    risk: RiskLevel = RiskLevel.LOW,
    timeout_seconds: float = 30,
    network_isolation: str = "enforced",
) -> ExecutionApprovalDetails:
    request = ExecutionRequest(
        mode=mode,  # type: ignore[arg-type]
        argv=("pytest", "-q") if mode == "exec" else None,
        script=None if mode == "exec" else "pytest -q && ruff check .",
        working_directory=Path.cwd().resolve(),
        limits=limits(timeout_seconds),
        network_access=False,
        filesystem_mode="workspace-write",
        shell_kind="auto" if mode == "exec" else "posix",
    )
    return ExecutionApprovalDetails(
        execution_id="exec_01",
        command_display=(
            "pytest -q" if mode == "exec" else "pytest -q && ruff check ."
        ),
        request=request,
        backend="container",
        capabilities=capabilities(network_isolation=network_isolation),
        risk=risk,
        reasons=("known project verification command",),
        policy_version="policy-v1",
    )


def request(
    *,
    call_id: str = "call_01",
    arguments: dict | None = None,
    session_id: str = "session_01",
    workspace_id: str = "workspace_01",
    execution: ExecutionApprovalDetails | None = None,
) -> ApprovalRequest:
    return ApprovalRequest.create(
        call_id=call_id,
        tool_name="shell" if execution is not None else "write_file",
        arguments=arguments or {"path": "notes.txt", "content": "hello"},
        identity=ApprovalIdentity(session_id, workspace_id),
        execution=execution,
    )


class RecordingHandler:
    def __init__(self, *responses: ApprovalResponse) -> None:
        self.responses = list(responses)
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, item: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(item)
        return self.responses.pop(0)


class ApprovalRequestTests(unittest.TestCase):
    def test_arguments_are_canonical_and_returned_as_defensive_copies(self):
        item = request(arguments={"z": 1, "a": {"nested": True}})

        first = item.arguments
        first["z"] = 99

        self.assertEqual(item.arguments_json, '{"a":{"nested":true},"z":1}')
        self.assertEqual(item.arguments["z"], 1)

    def test_fingerprint_ignores_call_identity_and_json_key_order(self):
        first = request(
            call_id="call_01",
            arguments={"path": "a", "content": "b"},
        )
        second = request(
            call_id="call_02",
            arguments={"content": "b", "path": "a"},
        )

        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_fingerprint_changes_with_arguments_workspace_or_security_contract(self):
        baseline = request(execution=execution_details())
        changed_arguments = request(
            arguments={"command": "pytest tests/unit"},
            execution=execution_details(),
        )
        changed_workspace = request(
            workspace_id="workspace_02",
            execution=execution_details(),
        )
        changed_limits = request(
            execution=execution_details(timeout_seconds=60),
        )
        changed_capability = request(
            execution=execution_details(network_isolation="best_effort"),
        )

        for changed in (
            changed_arguments,
            changed_workspace,
            changed_limits,
            changed_capability,
        ):
            with self.subTest(fingerprint=changed.fingerprint):
                self.assertNotEqual(baseline.fingerprint, changed.fingerprint)

    def test_execution_details_expose_exact_limits_and_capabilities(self):
        details = execution_details(timeout_seconds=45)
        item = request(execution=details)

        self.assertIs(item.execution, details)
        self.assertEqual(item.execution.effective_limits.timeout_seconds, 45)
        self.assertEqual(
            item.execution.capabilities.filesystem_isolation,
            "enforced",
        )
        self.assertEqual(item.execution.working_directory, Path.cwd().resolve())

    def test_unsafe_execution_only_allows_approve_once(self):
        shell = request(execution=execution_details(mode="shell"))
        high_risk = request(execution=execution_details(risk=RiskLevel.HIGH))

        self.assertEqual(shell.allowed_scopes, (ApprovalScope.ONCE,))
        self.assertEqual(high_risk.allowed_scopes, (ApprovalScope.ONCE,))

    def test_shell_tool_without_execution_details_still_cannot_persist(self):
        item = ApprovalRequest.create(
            call_id="call_01",
            tool_name="shell",
            arguments={"command": "echo hello"},
            identity=ApprovalIdentity("session_01", "workspace_01"),
        )

        self.assertEqual(item.allowed_scopes, (ApprovalScope.ONCE,))


class ApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_grant_reuses_only_the_exact_fingerprint(self):
        handler = RecordingHandler(
            ApprovalResponse.approve(ApprovalScope.SESSION),
            ApprovalResponse.reject(),
        )
        service = ApprovalService(handler)

        first = await service.authorize(request(call_id="call_01"))
        reused = await service.authorize(request(call_id="call_02"))
        changed = await service.authorize(
            request(call_id="call_03", arguments={"path": "other.txt"})
        )

        self.assertFalse(first.reused)
        self.assertTrue(reused.reused)
        self.assertEqual(reused.scope, ApprovalScope.SESSION)
        self.assertEqual(changed.decision, ApprovalDecision.REJECTED)
        self.assertEqual(len(handler.requests), 2)

    async def test_session_grant_does_not_cross_sessions(self):
        handler = RecordingHandler(
            ApprovalResponse.approve(ApprovalScope.SESSION),
            ApprovalResponse.reject(),
        )
        service = ApprovalService(handler)

        await service.authorize(request(session_id="session_01"))
        response = await service.authorize(request(session_id="session_02"))

        self.assertEqual(response.decision, ApprovalDecision.REJECTED)
        self.assertEqual(len(handler.requests), 2)

    async def test_workspace_grant_can_be_reused_by_another_session(self):
        handler = RecordingHandler(
            ApprovalResponse.approve(ApprovalScope.WORKSPACE)
        )
        service = ApprovalService(handler)

        await service.authorize(request(session_id="session_01"))
        response = await service.authorize(request(session_id="session_02"))

        self.assertTrue(response.reused)
        self.assertEqual(response.scope, ApprovalScope.WORKSPACE)
        self.assertEqual(len(handler.requests), 1)

    async def test_rejection_is_never_remembered(self):
        handler = RecordingHandler(
            ApprovalResponse.reject(),
            ApprovalResponse.reject(),
        )
        service = ApprovalService(handler)
        item = request()

        first = await service.authorize(item)
        second = await service.authorize(item)

        self.assertEqual(first.decision, ApprovalDecision.REJECTED)
        self.assertEqual(second.decision, ApprovalDecision.REJECTED)
        self.assertEqual(len(handler.requests), 2)

    async def test_handler_cannot_persist_an_unsafe_shell_scope(self):
        handler = RecordingHandler(
            ApprovalResponse.approve(ApprovalScope.WORKSPACE)
        )
        service = ApprovalService(handler)

        with self.assertRaisesRegex(ApprovalError, "unsafe"):
            await service.authorize(
                request(execution=execution_details(mode="shell"))
            )


class ExecutionApprovalGateTests(unittest.IsolatedAsyncioTestCase):
    def prepared(self) -> PreparedExecution:
        details = execution_details(mode="shell", risk=RiskLevel.HIGH)
        descriptor = BackendDescriptor(
            name="container",
            available=True,
            capabilities=details.capabilities,
            version="test",
            runtime=ContainerRuntimeInfo(
                name="docker",
                executable=ROOT / "docker",
                client_version="test",
                server_version="test",
                daemon_reachable=True,
                rootless="unknown",
            ),
        )
        return PreparedExecution(
            request=details.request,
            backend=descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(),
            ),
            resolved_shell="posix",
        )

    def context(self) -> ExecutionContext:
        return ExecutionContext(
            execution_id="exec_gate",
            tool_call_id="call_gate",
            session_id="session_gate",
            turn_id="turn_gate",
            workspace_id="workspace_gate",
            project_root=Path.cwd(),
            launched_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
        )

    def decision(self, *, approval: bool = True) -> PolicyDecision:
        return PolicyDecision(
            allowed=True,
            risk=RiskLevel.HIGH,
            requires_approval=approval,
            effective_limits=limits(),
            requirements=CapabilityRequirements(),
            reasons=(
                PolicyReason(
                    code="shell-script",
                    message="Shell syntax requires confirmation.",
                    rule_id="policy.shell",
                ),
            ),
        )

    async def test_builds_one_exact_approve_once_request(self):
        handler = RecordingHandler(ApprovalResponse.approve())
        gate = ExecutionApprovalGate(
            ApprovalService(handler),
            policy_version="policy-v7",
        )
        prepared = self.prepared()
        decision = self.decision()

        approved = await gate(prepared, decision, self.context())

        self.assertTrue(approved)
        self.assertEqual(len(handler.requests), 1)
        item = handler.requests[0]
        self.assertEqual(item.call_id, "call_gate")
        self.assertEqual(item.tool_name, "shell")
        self.assertEqual(item.allowed_scopes, (ApprovalScope.ONCE,))
        assert item.execution is not None
        self.assertIs(item.execution.request, prepared.request)
        self.assertEqual(item.execution.backend, "container")
        self.assertEqual(item.execution.risk, RiskLevel.HIGH)
        self.assertEqual(item.execution.policy_version, "policy-v7")
        self.assertEqual(
            item.execution.reasons,
            ("Shell syntax requires confirmation.",),
        )

    async def test_rejection_blocks_and_no_approval_decision_skips_handler(self):
        handler = RecordingHandler(ApprovalResponse.reject())
        gate = ExecutionApprovalGate(
            ApprovalService(handler),
            policy_version="policy-v7",
        )
        prepared = self.prepared()

        rejected = await gate(prepared, self.decision(), self.context())
        automatic = await gate(
            prepared,
            self.decision(approval=False),
            self.context(),
        )

        self.assertFalse(rejected)
        self.assertTrue(automatic)
        self.assertEqual(len(handler.requests), 1)


if __name__ == "__main__":
    unittest.main()
