from __future__ import annotations

from ..audit.models import BackendResourceIdentifier
from ..audit.recovery import RecoveryDisposition
from ..errors import AuditRecoveryError
from .container_dialects import parse_container_id
from .container_identity import read_facts, verify_container_identity
from .container_models import LABEL_SCHEMA_VERSION, PLAN_VERSION
from .container_runtime import ContainerRuntime


class ContainerRecoveryHandler:
    def __init__(
        self,
        runtime: ContainerRuntime,
        *,
        host_id: str,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        if not isinstance(runtime, ContainerRuntime):
            raise TypeError("runtime must implement ContainerRuntime")
        if not isinstance(host_id, str) or not host_id.strip():
            raise ValueError("host_id cannot be empty")
        if (
            isinstance(termination_grace_seconds, bool)
            or not isinstance(termination_grace_seconds, (int, float))
            or termination_grace_seconds < 0
        ):
            raise ValueError("termination_grace_seconds must not be negative")

        self._runtime = runtime
        self._host_id = host_id.strip()
        self._grace = float(termination_grace_seconds)

    async def recover(
        self,
        resource: BackendResourceIdentifier,
    ) -> RecoveryDisposition:
        facts = self._validate_resource(resource)

        try:
            inspection = await self._runtime.inspect(facts.container_id)
        except Exception as error:
            raise self._error(resource, "container inspection failed") from error

        if inspection is None:
            return RecoveryDisposition.RESOURCE_ABSENT

        mismatches = verify_container_identity(
            resource,
            inspection,
            host_id=self._host_id,
        )
        if mismatches:
            raise self._error(
                resource,
                "container recovery identity failed: " + ", ".join(mismatches),
            )

        try:
            if inspection.running:
                await self._runtime.stop(facts.container_id, self._grace)
                inspection = await self._runtime.inspect(facts.container_id)
                if inspection is not None and inspection.running:
                    await self._runtime.kill(facts.container_id)
                    inspection = await self._runtime.inspect(facts.container_id)
                    if inspection is not None and inspection.running:
                        raise self._error(
                            resource,
                            "the exact container survived forced termination",
                        )

            await self._runtime.remove(facts.container_id, force=True)
            if await self._runtime.inspect(facts.container_id) is not None:
                raise self._error(
                    resource,
                    "the exact container survived recovery removal",
                )
        except AuditRecoveryError:
            raise
        except Exception as error:
            raise self._error(resource, "container recovery cleanup failed") from error

        return RecoveryDisposition.TERMINATED

    def _validate_resource(self, resource: BackendResourceIdentifier):
        if not isinstance(resource, BackendResourceIdentifier):
            raise TypeError("resource must be a BackendResourceIdentifier")
        if resource.host_id != self._host_id:
            raise self._error(resource, "container recovery host identity failed")

        try:
            facts = read_facts(resource)
            parse_container_id(facts.container_id)
        except ValueError as error:
            raise self._error(
                resource,
                f"container recovery resource is invalid: {error}",
            ) from error

        descriptor = self._runtime.descriptor
        if descriptor.name != facts.runtime:
            raise self._error(resource, "container recovery runtime identity failed")

        current_version = (
            descriptor.server_version or descriptor.client_version or "unknown"
        )
        if current_version != facts.runtime_version:
            raise self._error(resource, "container recovery runtime version changed")
        if facts.label_schema != LABEL_SCHEMA_VERSION:
            raise self._error(
                resource, "container recovery label schema is unsupported"
            )
        if facts.plan_version != PLAN_VERSION:
            raise self._error(
                resource, "container recovery plan version is unsupported"
            )
        return facts

    @staticmethod
    def _error(
        resource: BackendResourceIdentifier,
        message: str,
    ) -> AuditRecoveryError:
        return AuditRecoveryError(
            message,
            execution_id=resource.resource_id,
            backend="container",
            operation="recover",
        )
