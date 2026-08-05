from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.audit.recovery import RecoveryDisposition
from truecoder.execution.backends.container_models import (
    LABEL_AUDIT_RUN_ID,
    LABEL_EXECUTION_ID,
    LABEL_IMAGE_DIGEST,
    LABEL_MANAGED,
    LABEL_OWNERSHIP_TOKEN,
    LABEL_SCHEMA,
    LABEL_SCHEMA_VERSION,
    ContainerInspection,
)
from truecoder.execution.backends.container_recovery import ContainerRecoveryHandler
from truecoder.execution.backends.models import ContainerRuntimeInfo
from truecoder.execution.errors import AuditRecoveryError

CONTAINER_ID = "c" * 64
OTHER_CONTAINER_ID = "d" * 64
DIGEST = "sha256:" + "a" * 64
TOKEN = "token-recovery"
ROOT = Path.cwd().resolve()


def resource(**detail_overrides: str) -> BackendResourceIdentifier:
    details = {
        "runtime": "docker",
        "runtime_version": "29.3.0",
        "container_id": CONTAINER_ID,
        "audit_run_id": "run-recovery",
        "image_digest": DIGEST,
        "label_schema": "1",
        "plan_version": "1",
    }
    details.update(detail_overrides)
    return BackendResourceIdentifier(
        version=1,
        backend="container",
        resource_kind="container",
        resource_id="exec-recovery",
        ownership_token=TOKEN,
        host_id="host-recovery",
        created_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
        native_details=tuple(sorted(details.items())),
    )


def inspection(
    *,
    state: str = "running",
    labels: dict[str, str] | None = None,
    container_id: str = CONTAINER_ID,
) -> ContainerInspection:
    expected_labels = {
        LABEL_MANAGED: "true",
        LABEL_EXECUTION_ID: "exec-recovery",
        LABEL_AUDIT_RUN_ID: "run-recovery",
        LABEL_OWNERSHIP_TOKEN: TOKEN,
        LABEL_IMAGE_DIGEST: DIGEST,
        LABEL_SCHEMA: LABEL_SCHEMA_VERSION,
    }
    if labels is not None:
        expected_labels.update(labels)
    return ContainerInspection(
        container_id=container_id,
        state=state,  # type: ignore[arg-type]
        labels=tuple(sorted(expected_labels.items())),
        image_digest=DIGEST,
        exit_code=0 if state in {"exited", "dead"} else None,
    )


class FakeRuntime:
    def __init__(
        self,
        inspections: list[ContainerInspection | None],
        *,
        version: str = "29.3.0",
    ) -> None:
        self._descriptor = ContainerRuntimeInfo(
            name="docker",
            executable=ROOT / "docker",
            client_version=version,
            server_version=version,
            daemon_reachable=True,
            rootless="yes",
        )
        self.inspections = inspections
        self.calls: list[tuple] = []

    @property
    def descriptor(self) -> ContainerRuntimeInfo:
        return self._descriptor

    async def create(self, plan):
        del plan
        raise AssertionError("recovery must not create containers")

    async def start_attached(self, container_id):
        del container_id
        raise AssertionError("recovery must not start containers")

    async def inspect(self, container_id):
        self.calls.append(("inspect", container_id))
        if not self.inspections:
            return None
        return self.inspections.pop(0)

    async def stop(self, container_id, grace_seconds):
        self.calls.append(("stop", container_id, grace_seconds))

    async def kill(self, container_id):
        self.calls.append(("kill", container_id))

    async def remove(self, container_id, *, force):
        self.calls.append(("remove", container_id, force))


class ContainerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_absent_exact_resource_is_not_mutated(self):
        runtime = FakeRuntime([None])
        handler = ContainerRecoveryHandler(runtime, host_id="host-recovery")

        result = await handler.recover(resource())

        self.assertIs(result, RecoveryDisposition.RESOURCE_ABSENT)
        self.assertEqual(runtime.calls, [("inspect", CONTAINER_ID)])

    async def test_a_running_exact_resource_is_stopped_and_removed(self):
        runtime = FakeRuntime([inspection(), inspection(state="exited"), None])
        handler = ContainerRecoveryHandler(
            runtime,
            host_id="host-recovery",
            termination_grace_seconds=2.0,
        )

        result = await handler.recover(resource())

        self.assertIs(result, RecoveryDisposition.TERMINATED)
        self.assertIn(("stop", CONTAINER_ID, 2.0), runtime.calls)
        self.assertIn(("remove", CONTAINER_ID, True), runtime.calls)

    async def test_a_stubborn_exact_resource_is_force_killed(self):
        runtime = FakeRuntime(
            [
                inspection(),
                inspection(),
                inspection(state="exited"),
                None,
            ]
        )
        handler = ContainerRecoveryHandler(runtime, host_id="host-recovery")

        result = await handler.recover(resource())

        self.assertIs(result, RecoveryDisposition.TERMINATED)
        self.assertIn(("kill", CONTAINER_ID), runtime.calls)

    async def test_an_identity_mismatch_fails_before_mutation(self):
        runtime = FakeRuntime(
            [inspection(labels={LABEL_OWNERSHIP_TOKEN: "other-token"})]
        )
        handler = ContainerRecoveryHandler(runtime, host_id="host-recovery")

        with self.assertRaises(AuditRecoveryError):
            await handler.recover(resource())

        self.assertEqual(runtime.calls, [("inspect", CONTAINER_ID)])

    async def test_host_and_runtime_mismatches_fail_before_inspection(self):
        cases = (
            (
                ContainerRecoveryHandler(
                    FakeRuntime([]),
                    host_id="other-host",
                ),
                resource(),
            ),
            (
                ContainerRecoveryHandler(
                    FakeRuntime([], version="30.0.0"),
                    host_id="host-recovery",
                ),
                resource(),
            ),
        )

        for handler, item in cases:
            with self.subTest(handler=handler), self.assertRaises(AuditRecoveryError):
                await handler.recover(item)

    async def test_invalid_full_id_and_protocol_versions_fail_closed(self):
        runtime = FakeRuntime([])
        handler = ContainerRecoveryHandler(runtime, host_id="host-recovery")

        for item in (
            resource(container_id="short"),
            resource(label_schema="2"),
            resource(plan_version="2"),
        ):
            with self.subTest(item=item), self.assertRaises(AuditRecoveryError):
                await handler.recover(item)

        self.assertEqual(runtime.calls, [])

    async def test_a_container_surviving_removal_is_recovery_failure(self):
        runtime = FakeRuntime(
            [
                inspection(state="exited"),
                inspection(state="exited"),
            ]
        )
        handler = ContainerRecoveryHandler(runtime, host_id="host-recovery")

        with self.assertRaises(AuditRecoveryError):
            await handler.recover(resource())

        self.assertIn(("remove", CONTAINER_ID, True), runtime.calls)


if __name__ == "__main__":
    unittest.main()
