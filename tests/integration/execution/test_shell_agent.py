from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.agent import (
    Agent,
    AgentEventType,
    ApprovalRequest,
    ApprovalResponse,
    ContextBuilder,
)
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.execution.audit import AuditRunPhase, TerminalOutcome
from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoveredProgram,
    DiscoverySnapshot,
    HostPlatformInfo,
    UnavailableReason,
)
from truecoder.execution.bootstrap import (
    ExecutionBootstrapConfig,
    bootstrap_execution,
)
from truecoder.execution.models import BackendCapabilities
from truecoder.tools import ToolCall, ToolRegistry
from truecoder.tools.builtin import ShellTool


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


class ScriptedClient:
    def __init__(self, command: tuple[str, ...]) -> None:
        self.command = command
        self.calls = []

    async def chat_completion(self, messages, stream=True, tools=None):
        index = len(self.calls)
        self.calls.append((messages, stream, tools))
        if index == 0:
            arguments = json.dumps(
                {
                    "argv": self.command,
                    "filesystem_mode": "host",
                    "network_access": True,
                }
            )
            yield StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                tool_calls=(
                    ToolCall("call-shell-agent", "shell", arguments),
                ),
                finish_reason="tool_calls",
            )
            return
        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=TextDelta("The command failed as expected."),
            finish_reason="stop",
        )

    async def close(self) -> None:
        return None


class RecordingApprovals:
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse.approve()


def posix_snapshot() -> DiscoverySnapshot:
    executable = Path(sys.executable).resolve()
    shell = Path("/bin/sh")
    return DiscoverySnapshot(
        host=HostPlatformInfo(
            system="linux",
            family="posix",
            architecture="integration",
        ),
        shells=(
            DiscoveredProgram(
                name="sh",
                path=shell,
                shell_kind="posix",
            ),
        ),
        cgroup_v2=None,
        runtimes=(),
        backends=(
            BackendDescriptor(
                name="posix",
                available=True,
                capabilities=BackendCapabilities(
                    filesystem_isolation="unsupported",
                    network_isolation="unsupported",
                    memory_limits="best_effort",
                    cpu_limits="best_effort",
                    process_limits="best_effort",
                    timeout_enforcement="enforced",
                    cancellation="enforced",
                    supported_execution_modes=("exec", "shell"),
                    supported_filesystem_modes=("host",),
                    supported_shells=("posix",),
                ),
                version=f"integration:{executable.name}",
            ),
            BackendDescriptor(
                name="windows",
                available=False,
                capabilities=BackendCapabilities(
                    filesystem_isolation="unsupported",
                    network_isolation="unsupported",
                    memory_limits="unsupported",
                    cpu_limits="unsupported",
                    process_limits="unsupported",
                    timeout_enforcement="unsupported",
                    cancellation="unsupported",
                    supported_execution_modes=("exec",),
                    supported_filesystem_modes=("host",),
                    supported_shells=(),
                ),
                unavailable_reasons=(
                    UnavailableReason(
                        code="not_host_platform",
                        message="not the host platform",
                    ),
                ),
            ),
            BackendDescriptor(
                name="container",
                available=False,
                capabilities=BackendCapabilities(
                    filesystem_isolation="unsupported",
                    network_isolation="unsupported",
                    memory_limits="unsupported",
                    cpu_limits="unsupported",
                    process_limits="unsupported",
                    timeout_enforcement="unsupported",
                    cancellation="unsupported",
                    supported_execution_modes=("exec",),
                    supported_filesystem_modes=("host",),
                    supported_shells=(),
                ),
                unavailable_reasons=(
                    UnavailableReason(
                        code="runtime_missing",
                        message="runtime missing",
                    ),
                ),
            ),
        ),
    )


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class ShellAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_receives_nonzero_output_with_durable_audit_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            approvals = RecordingApprovals()
            client = ScriptedClient(
                (
                    sys.executable,
                    "-c",
                    (
                        "import sys; print('agent-out'); "
                        "print('agent-err', file=sys.stderr); "
                        "raise SystemExit(7)"
                    ),
                )
            )
            registry = ToolRegistry()
            agent = Agent(
                llm_client=client,  # type: ignore[arg-type]
                context_builder=ContextBuilder(
                    system_prompt="test system",
                    max_input_tokens=1000,
                    token_counter=FixedTokenCounter(),
                ),
                tool_registry=registry,
                approval_handler=approvals,
                project_root=root,
            )
            runtime = await bootstrap_execution(
                agent.approval_service,
                config=ExecutionBootstrapConfig(
                    audit_database_path=root / "audit.sqlite3",
                    image_lock_path=root / "missing-image.lock",
                ),
                discovery_snapshot=posix_snapshot(),
            )
            assert runtime.service is not None
            assert runtime.audit is not None
            registry.register(ShellTool(root, runtime.service))

            events = [event async for event in agent.run("Run the command")]

            result_event = next(
                event
                for event in events
                if event.type is AgentEventType.TOOL_RESULT
            )
            tool_payload = json.loads(result_event.data["content"])
            output = tool_payload["output"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(output["exit_code"], 7)
            self.assertEqual(output["stdout"], "agent-out\n")
            self.assertEqual(output["stderr"], "agent-err\n")
            self.assertEqual(output["backend"], "posix")
            self.assertTrue(output["audit_id"])

            self.assertEqual(len(approvals.requests), 1)
            approval = approvals.requests[0]
            self.assertIsNotNone(approval.execution)
            self.assertEqual(approval.call_id, "call-shell-agent")
            self.assertEqual(approval.allowed_scopes[0].value, "once")
            assert approval.execution is not None
            self.assertEqual(
                approval.execution.request.argv,
                client.command,
            )

            snapshot = await runtime.audit.get_run(output["audit_id"])
            self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)
            assert snapshot.record.finalization is not None
            self.assertIs(
                snapshot.record.finalization.outcome,
                TerminalOutcome.FAILED,
            )
            self.assertEqual(events[-1].type, AgentEventType.AGENT_END)


if __name__ == "__main__":
    unittest.main()
