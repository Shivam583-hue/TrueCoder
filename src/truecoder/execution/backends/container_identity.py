from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ..audit.models import BackendResourceIdentifier
from .container_models import (
    LABEL_AUDIT_RUN_ID,
    LABEL_EXECUTION_ID,
    LABEL_IMAGE_DIGEST,
    LABEL_MANAGED,
    LABEL_OWNERSHIP_TOKEN,
    LABEL_SCHEMA,
    LABEL_SCHEMA_VERSION,
    ContainerCreatePlan,
    ContainerInspection,
)
from .models import ContainerRuntimeName

RESOURCE_KIND: Final = "container"
DETAIL_RUNTIME: Final = "runtime"
DETAIL_RUNTIME_VERSION: Final = "runtime_version"
DETAIL_CONTAINER_ID: Final = "container_id"
DETAIL_AUDIT_RUN_ID: Final = "audit_run_id"
DETAIL_IMAGE_DIGEST: Final = "image_digest"
DETAIL_LABEL_SCHEMA: Final = "label_schema"
DETAIL_PLAN_VERSION: Final = "plan_version"

REQUIRED_DETAILS: Final = (
    DETAIL_RUNTIME,
    DETAIL_RUNTIME_VERSION,
    DETAIL_CONTAINER_ID,
    DETAIL_AUDIT_RUN_ID,
    DETAIL_IMAGE_DIGEST,
    DETAIL_LABEL_SCHEMA,
    DETAIL_PLAN_VERSION,
)


@dataclass(frozen=True, slots=True)
class ContainerResourceFacts:
    runtime: ContainerRuntimeName
    runtime_version: str
    container_id: str
    audit_run_id: str
    image_digest: str
    label_schema: str = LABEL_SCHEMA_VERSION
    plan_version: str = "1"

    def as_details(self) -> tuple[tuple[str, str], ...]:
        return (
            (DETAIL_RUNTIME, self.runtime),
            (DETAIL_RUNTIME_VERSION, self.runtime_version),
            (DETAIL_CONTAINER_ID, self.container_id),
            (DETAIL_AUDIT_RUN_ID, self.audit_run_id),
            (DETAIL_IMAGE_DIGEST, self.image_digest),
            (DETAIL_LABEL_SCHEMA, self.label_schema),
            (DETAIL_PLAN_VERSION, self.plan_version),
        )


def create_container_resource(
    plan: ContainerCreatePlan,
    *,
    container_id: str,
    runtime_version: str,
    host_id: str,
    created_at_utc: datetime | None = None,
) -> BackendResourceIdentifier:
    facts = ContainerResourceFacts(
        runtime=plan.runtime,
        runtime_version=runtime_version,
        container_id=container_id,
        audit_run_id=plan.labels.audit_run_id,
        image_digest=plan.image.digest,
        label_schema=plan.labels.schema_version,
        plan_version=plan.plan_version,
    )
    return BackendResourceIdentifier(
        version=1,
        backend="container",
        resource_kind=RESOURCE_KIND,
        resource_id=plan.labels.execution_id,
        ownership_token=plan.labels.ownership_token,
        host_id=host_id,
        created_at_utc=created_at_utc or datetime.now(UTC),
        native_details=facts.as_details(),
    )


def read_facts(resource: BackendResourceIdentifier) -> ContainerResourceFacts:
    if resource.backend != "container":
        raise ValueError("resource is not a container resource")
    if resource.resource_kind != RESOURCE_KIND:
        raise ValueError("resource kind is not a container")

    details = dict(resource.native_details)
    missing = tuple(name for name in REQUIRED_DETAILS if name not in details)
    if missing:
        raise ValueError(
            f"container resource is missing details: {', '.join(missing)}"
        )
    runtime = details[DETAIL_RUNTIME]
    if runtime != "docker":
        raise ValueError(f"unsupported container runtime: {runtime!r}")

    return ContainerResourceFacts(
        runtime="docker",
        runtime_version=details[DETAIL_RUNTIME_VERSION],
        container_id=details[DETAIL_CONTAINER_ID],
        audit_run_id=details[DETAIL_AUDIT_RUN_ID],
        image_digest=details[DETAIL_IMAGE_DIGEST],
        label_schema=details[DETAIL_LABEL_SCHEMA],
        plan_version=details[DETAIL_PLAN_VERSION],
    )


def verify_container_identity(
    resource: BackendResourceIdentifier,
    inspection: ContainerInspection,
    *,
    host_id: str,
) -> tuple[str, ...]:
    mismatches: list[str] = []

    if resource.host_id != host_id:
        mismatches.append("host")

    try:
        facts = read_facts(resource)
    except ValueError as error:
        return (str(error),)

    if facts.container_id != inspection.container_id:
        mismatches.append("container-id")
    if facts.image_digest != inspection.image_digest:
        mismatches.append("image-digest")

    labels = inspection.label_map()
    if labels.get(LABEL_MANAGED) != "true":
        mismatches.append("managed-label")
    if labels.get(LABEL_EXECUTION_ID) != resource.resource_id:
        mismatches.append("execution-id-label")
    if labels.get(LABEL_OWNERSHIP_TOKEN) != resource.ownership_token:
        mismatches.append("ownership-token-label")
    if labels.get(LABEL_AUDIT_RUN_ID) != facts.audit_run_id:
        mismatches.append("audit-run-id-label")
    if labels.get(LABEL_IMAGE_DIGEST) != facts.image_digest:
        mismatches.append("image-digest-label")
    if labels.get(LABEL_SCHEMA) != facts.label_schema:
        mismatches.append("label-schema")

    return tuple(mismatches)
