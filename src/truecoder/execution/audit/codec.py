from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Final, TypeAlias

from truecoder.execution.errors import ExecutionSerializationError

from .models import (
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
    Metadata,
    OutputEvidence,
    RecoveryResult,
    TerminalOutcome,
)

AUDIT_CODEC_VERSION: Final = 1

AuditModel: TypeAlias = (
    AuditRunAdmission
    | AuditRunHandle
    | BackendResourceIdentifier
    | OutputEvidence
    | AuditRunStart
    | RecoveryResult
    | AuditFinalization
    | AuditEvent
    | AuditRunRecord
    | AuditRunSnapshot
)
JsonObject: TypeAlias = dict[str, object]

_MODEL_NAMES: Final = {
    AuditRunAdmission: "audit_run_admission",
    AuditRunHandle: "audit_run_handle",
    BackendResourceIdentifier: "backend_resource_identifier",
    OutputEvidence: "output_evidence",
    AuditRunStart: "audit_run_start",
    RecoveryResult: "recovery_result",
    AuditFinalization: "audit_finalization",
    AuditEvent: "audit_event",
    AuditRunRecord: "audit_run_record",
    AuditRunSnapshot: "audit_run_snapshot",
}


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ExecutionSerializationError(
            "audit data must be JSON serializable",
            operation="serialize_audit",
        ) from error


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialize_audit_model(model: AuditModel) -> str:
    model_name = _MODEL_NAMES.get(type(model))
    if model_name is None:
        raise TypeError(f"unsupported audit model: {type(model).__name__}")
    return canonical_json(
        {
            "data": _encode_model(model),
            "model": model_name,
            "version": AUDIT_CODEC_VERSION,
        }
    )


def deserialize_audit_model(payload: str) -> AuditModel:
    if not isinstance(payload, str):
        raise TypeError("payload must be a string")
    if not payload.strip():
        raise ExecutionSerializationError(
            "serialized audit payload cannot be empty",
            operation="deserialize_audit",
        )

    try:
        raw = json.loads(payload)
        envelope = _object(raw, "payload")
        _exact(envelope, {"data", "model", "version"}, "payload")
        version = _integer(envelope["version"], "payload.version")
        if version != AUDIT_CODEC_VERSION:
            raise ValueError(f"unsupported audit codec version: {version}")
        model_name = _string(envelope["model"], "payload.model")
        return _decode_model(model_name, _object(envelope["data"], "payload.data"))
    except ExecutionSerializationError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExecutionSerializationError(
            f"invalid serialized audit payload: {error}",
            operation="deserialize_audit",
        ) from error


def _encode_model(model: AuditModel) -> JsonObject:
    if isinstance(model, AuditRunAdmission):
        return {
            "created_at": _datetime(model.created_at),
            "execution_id": model.execution_id,
            "request_sha256": model.request_sha256,
            "request_summary": _metadata(model.request_summary),
            "run_id": model.run_id,
            "session_id": model.session_id,
            "tool_call_id": model.tool_call_id,
            "turn_id": model.turn_id,
            "workspace_id": model.workspace_id,
        }
    if isinstance(model, AuditRunHandle):
        return {
            "execution_id": model.execution_id,
            "run_id": model.run_id,
        }
    if isinstance(model, BackendResourceIdentifier):
        return _encode_resource(model)
    if isinstance(model, OutputEvidence):
        return _encode_output(model)
    if isinstance(model, AuditRunStart):
        return {
            "metadata": _metadata(model.metadata),
            "resource": (
                _encode_resource(model.resource) if model.resource is not None else None
            ),
            "run_id": model.run_id,
            "started_at": _datetime(model.started_at),
        }
    if isinstance(model, RecoveryResult):
        return {
            "attempted_at": _datetime(model.attempted_at),
            "detail": model.detail,
            "outcome": model.outcome.value,
            "previous_phase": model.previous_phase.value,
            "resource": (
                _encode_resource(model.resource) if model.resource is not None else None
            ),
            "run_id": model.run_id,
        }
    if isinstance(model, AuditFinalization):
        return {
            "command_started": model.command_started,
            "detail": model.detail,
            "exit_code": model.exit_code,
            "finalized_at": _datetime(model.finalized_at),
            "outcome": model.outcome.value,
            "output": (
                _encode_output(model.output) if model.output is not None else None
            ),
            "recovery": (
                _encode_model(model.recovery) if model.recovery is not None else None
            ),
            "resource": (
                _encode_resource(model.resource) if model.resource is not None else None
            ),
            "run_id": model.run_id,
            "underlying_outcome": (
                model.underlying_outcome.value
                if model.underlying_outcome is not None
                else None
            ),
        }
    if isinstance(model, AuditEvent):
        return {
            "event_id": model.event_id,
            "event_type": model.event_type.value,
            "message": model.message,
            "metadata": _metadata(model.metadata),
            "occurred_at": _datetime(model.occurred_at),
            "phase": model.phase.value,
            "run_id": model.run_id,
            "sequence": model.sequence,
            "terminal": model.terminal,
        }
    if isinstance(model, AuditRunRecord):
        return {
            "created_at": _datetime(model.created_at),
            "finalization": (
                _encode_model(model.finalization)
                if model.finalization is not None
                else None
            ),
            "phase": model.phase.value,
            "revision": model.revision,
            "run_id": model.run_id,
            "start": (_encode_model(model.start) if model.start is not None else None),
            "updated_at": _datetime(model.updated_at),
        }
    if isinstance(model, AuditRunSnapshot):
        return {
            "admission": _encode_model(model.admission),
            "record": _encode_model(model.record),
            "recovery_lease_until": (
                _datetime(model.recovery_lease_until)
                if model.recovery_lease_until is not None
                else None
            ),
            "recovery_owner": model.recovery_owner,
            "resource": (
                _encode_resource(model.resource) if model.resource is not None else None
            ),
        }
    raise TypeError(f"unsupported audit model: {type(model).__name__}")


def _decode_model(model_name: str, data: JsonObject) -> AuditModel:
    if model_name == "audit_run_admission":
        _exact(
            data,
            {
                "created_at",
                "execution_id",
                "request_sha256",
                "request_summary",
                "run_id",
                "session_id",
                "tool_call_id",
                "turn_id",
                "workspace_id",
            },
            model_name,
        )
        return AuditRunAdmission(
            run_id=_string(data["run_id"], "run_id"),
            execution_id=_string(data["execution_id"], "execution_id"),
            tool_call_id=_string(data["tool_call_id"], "tool_call_id"),
            session_id=_string(data["session_id"], "session_id"),
            turn_id=_string(data["turn_id"], "turn_id"),
            workspace_id=_string(data["workspace_id"], "workspace_id"),
            request_sha256=_string(data["request_sha256"], "request_sha256"),
            request_summary=_decode_metadata(
                data["request_summary"],
                "request_summary",
            ),
            created_at=_decode_datetime(data["created_at"], "created_at"),
        )
    if model_name == "audit_run_handle":
        _exact(data, {"execution_id", "run_id"}, model_name)
        return AuditRunHandle(
            run_id=_string(data["run_id"], "run_id"),
            execution_id=_string(data["execution_id"], "execution_id"),
        )
    if model_name == "backend_resource_identifier":
        return _decode_resource(data)
    if model_name == "output_evidence":
        return _decode_output(data)
    if model_name == "audit_run_start":
        _exact(data, {"metadata", "resource", "run_id", "started_at"}, model_name)
        return AuditRunStart(
            run_id=_string(data["run_id"], "run_id"),
            started_at=_decode_datetime(data["started_at"], "started_at"),
            resource=_decode_optional_resource(data["resource"]),
            metadata=_decode_metadata(data["metadata"], "metadata"),
        )
    if model_name == "recovery_result":
        return _decode_recovery(data)
    if model_name == "audit_finalization":
        _exact(
            data,
            {
                "command_started",
                "detail",
                "exit_code",
                "finalized_at",
                "outcome",
                "output",
                "recovery",
                "resource",
                "run_id",
                "underlying_outcome",
            },
            model_name,
        )
        recovery_data = data["recovery"]
        return AuditFinalization(
            run_id=_string(data["run_id"], "run_id"),
            finalized_at=_decode_datetime(data["finalized_at"], "finalized_at"),
            outcome=TerminalOutcome(_string(data["outcome"], "outcome")),
            command_started=_optional_bool(
                data["command_started"],
                "command_started",
            ),
            exit_code=_optional_integer(data["exit_code"], "exit_code"),
            output=(
                _decode_output(_object(data["output"], "output"))
                if data["output"] is not None
                else None
            ),
            resource=_decode_optional_resource(data["resource"]),
            underlying_outcome=(
                TerminalOutcome(
                    _string(data["underlying_outcome"], "underlying_outcome")
                )
                if data["underlying_outcome"] is not None
                else None
            ),
            recovery=(
                _decode_recovery(_object(recovery_data, "recovery"))
                if recovery_data is not None
                else None
            ),
            detail=_optional_string(data["detail"], "detail"),
        )
    if model_name == "audit_event":
        _exact(
            data,
            {
                "event_id",
                "event_type",
                "message",
                "metadata",
                "occurred_at",
                "phase",
                "run_id",
                "sequence",
                "terminal",
            },
            model_name,
        )
        return AuditEvent(
            event_id=_string(data["event_id"], "event_id"),
            run_id=_string(data["run_id"], "run_id"),
            sequence=_integer(data["sequence"], "sequence"),
            occurred_at=_decode_datetime(data["occurred_at"], "occurred_at"),
            phase=AuditRunPhase(_string(data["phase"], "phase")),
            event_type=AuditEventType(_string(data["event_type"], "event_type")),
            message=_optional_string(data["message"], "message"),
            metadata=_decode_metadata(data["metadata"], "metadata"),
            terminal=_boolean(data["terminal"], "terminal"),
        )
    if model_name == "audit_run_record":
        _exact(
            data,
            {
                "created_at",
                "finalization",
                "phase",
                "revision",
                "run_id",
                "start",
                "updated_at",
            },
            model_name,
        )
        start_data = data["start"]
        finalization_data = data["finalization"]
        start = (
            _decode_model(
                "audit_run_start",
                _object(start_data, "start"),
            )
            if start_data is not None
            else None
        )
        finalization = (
            _decode_model(
                "audit_finalization",
                _object(finalization_data, "finalization"),
            )
            if finalization_data is not None
            else None
        )
        if start is not None and not isinstance(start, AuditRunStart):
            raise TypeError("decoded start has the wrong model type")
        if finalization is not None and not isinstance(
            finalization,
            AuditFinalization,
        ):
            raise TypeError("decoded finalization has the wrong model type")
        return AuditRunRecord(
            run_id=_string(data["run_id"], "run_id"),
            created_at=_decode_datetime(data["created_at"], "created_at"),
            updated_at=_decode_datetime(data["updated_at"], "updated_at"),
            phase=AuditRunPhase(_string(data["phase"], "phase")),
            start=start,
            finalization=finalization,
            revision=_integer(data["revision"], "revision"),
        )
    if model_name == "audit_run_snapshot":
        _exact(
            data,
            {
                "admission",
                "record",
                "recovery_lease_until",
                "recovery_owner",
                "resource",
            },
            model_name,
        )
        admission = _decode_model(
            "audit_run_admission",
            _object(data["admission"], "admission"),
        )
        record = _decode_model(
            "audit_run_record",
            _object(data["record"], "record"),
        )
        if not isinstance(admission, AuditRunAdmission):
            raise TypeError("decoded admission has the wrong model type")
        if not isinstance(record, AuditRunRecord):
            raise TypeError("decoded record has the wrong model type")
        return AuditRunSnapshot(
            admission=admission,
            record=record,
            resource=_decode_optional_resource(data["resource"]),
            recovery_owner=_optional_string(
                data["recovery_owner"],
                "recovery_owner",
            ),
            recovery_lease_until=(
                _decode_datetime(
                    data["recovery_lease_until"],
                    "recovery_lease_until",
                )
                if data["recovery_lease_until"] is not None
                else None
            ),
        )
    raise ValueError(f"unknown audit model: {model_name!r}")


def _encode_resource(resource: BackendResourceIdentifier) -> JsonObject:
    return {
        "backend": resource.backend,
        "created_at_utc": _datetime(resource.created_at_utc),
        "host_id": resource.host_id,
        "native_details": _metadata(resource.native_details),
        "ownership_token": resource.ownership_token,
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "version": resource.version,
    }


def _decode_resource(data: JsonObject) -> BackendResourceIdentifier:
    _exact(
        data,
        {
            "backend",
            "created_at_utc",
            "host_id",
            "native_details",
            "ownership_token",
            "resource_id",
            "resource_kind",
            "version",
        },
        "backend_resource_identifier",
    )
    return BackendResourceIdentifier(
        version=_integer(data["version"], "version"),
        backend=_string(data["backend"], "backend"),
        resource_kind=_string(data["resource_kind"], "resource_kind"),
        resource_id=_string(data["resource_id"], "resource_id"),
        ownership_token=_string(data["ownership_token"], "ownership_token"),
        host_id=_string(data["host_id"], "host_id"),
        created_at_utc=_decode_datetime(
            data["created_at_utc"],
            "created_at_utc",
        ),
        native_details=_decode_metadata(data["native_details"], "native_details"),
    )


def _decode_optional_resource(
    value: object,
) -> BackendResourceIdentifier | None:
    if value is None:
        return None
    return _decode_resource(_object(value, "resource"))


def _encode_output(output: OutputEvidence) -> JsonObject:
    return {
        "complete": output.complete,
        "stderr_bytes": output.stderr_bytes,
        "stderr_preview": output.stderr_preview,
        "stderr_sha256": output.stderr_sha256,
        "stderr_truncated": output.stderr_truncated,
        "stdout_bytes": output.stdout_bytes,
        "stdout_preview": output.stdout_preview,
        "stdout_sha256": output.stdout_sha256,
        "stdout_truncated": output.stdout_truncated,
    }


def _decode_output(data: JsonObject) -> OutputEvidence:
    _exact(
        data,
        {
            "complete",
            "stderr_bytes",
            "stderr_preview",
            "stderr_sha256",
            "stderr_truncated",
            "stdout_bytes",
            "stdout_preview",
            "stdout_sha256",
            "stdout_truncated",
        },
        "output_evidence",
    )
    return OutputEvidence(
        stdout_sha256=_optional_string(data["stdout_sha256"], "stdout_sha256"),
        stderr_sha256=_optional_string(data["stderr_sha256"], "stderr_sha256"),
        stdout_bytes=_integer(data["stdout_bytes"], "stdout_bytes"),
        stderr_bytes=_integer(data["stderr_bytes"], "stderr_bytes"),
        stdout_preview=_string(data["stdout_preview"], "stdout_preview"),
        stderr_preview=_string(data["stderr_preview"], "stderr_preview"),
        stdout_truncated=_boolean(data["stdout_truncated"], "stdout_truncated"),
        stderr_truncated=_boolean(data["stderr_truncated"], "stderr_truncated"),
        complete=_boolean(data["complete"], "complete"),
    )


def _decode_recovery(data: JsonObject) -> RecoveryResult:
    _exact(
        data,
        {
            "attempted_at",
            "detail",
            "outcome",
            "previous_phase",
            "resource",
            "run_id",
        },
        "recovery_result",
    )
    return RecoveryResult(
        run_id=_string(data["run_id"], "run_id"),
        previous_phase=AuditRunPhase(_string(data["previous_phase"], "previous_phase")),
        attempted_at=_decode_datetime(data["attempted_at"], "attempted_at"),
        outcome=TerminalOutcome(_string(data["outcome"], "outcome")),
        resource=_decode_optional_resource(data["resource"]),
        detail=_optional_string(data["detail"], "detail"),
    )


def _metadata(value: Metadata) -> list[list[str]]:
    return [[key, item_value] for key, item_value in value]


def _decode_metadata(value: object, name: str) -> Metadata:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 2:
            raise TypeError(f"{name}[{index}] must be a two-item array")
        result.append(
            (
                _string(item[0], f"{name}[{index}][0]"),
                _string(item[1], f"{name}[{index}][1]"),
            )
        )
    return tuple(result)


def _datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _decode_datetime(value: object, name: str) -> datetime:
    text = _string(value, name)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from error


def _object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


def _exact(value: JsonObject, fields: set[str], name: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise ValueError(f"{name} fields differ; missing={missing}, extra={extra}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, name)
