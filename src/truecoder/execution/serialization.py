from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final, TypeAlias

from .errors import ExecutionSerializationError
from .models import (
    BackendCapabilities,
    CapabilityCheck,
    ExecutionContext,
    ExecutionLifecycleEvent,
    ExecutionLimits,
    ExecutionRequest,
    ExecutionResult,
    NativeDiagnostic,
    PolicyDecision,
)

SERIALIZATION_VERSION: Final = 2

ExecutionModel: TypeAlias = (
    ExecutionLimits
    | ExecutionRequest
    | PolicyDecision
    | BackendCapabilities
    | CapabilityCheck
    | ExecutionResult
    | ExecutionContext
    | NativeDiagnostic
    | ExecutionLifecycleEvent
)

JsonObject: TypeAlias = dict[str, object]

_MODEL_NAMES: Final = {
    ExecutionLimits: "execution_limits",
    ExecutionRequest: "execution_request",
    PolicyDecision: "policy_decision",
    BackendCapabilities: "backend_capabilities",
    CapabilityCheck: "capability_check",
    ExecutionResult: "execution_result",
    ExecutionContext: "execution_context",
    NativeDiagnostic: "native_diagnostic",
    ExecutionLifecycleEvent: "execution_lifecycle_event",
}

_NATIVE_PATH_FLAVOR: Final = "windows" if os.name == "nt" else "posix"


def serialize_execution_model(model: ExecutionModel) -> str:
    """Serialize one execution domain model into a versioned JSON envelope."""

    model_name = _MODEL_NAMES.get(type(model))
    if model_name is None:
        raise TypeError(f"unsupported execution model: {type(model).__name__}")

    envelope = {
        "data": _encode_model(model),
        "model": model_name,
        "version": SERIALIZATION_VERSION,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def deserialize_execution_model(payload: str) -> ExecutionModel:
    """Deserialize and validate one versioned execution domain model."""

    if not isinstance(payload, str):
        raise TypeError("payload must be a string")
    if not payload.strip():
        raise ExecutionSerializationError(
            "serialized execution payload cannot be empty",
            operation="deserialize",
        )

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ExecutionSerializationError(
            "serialized execution payload is not valid JSON",
            operation="deserialize",
        ) from exc

    try:
        envelope = _require_object(raw, "payload")
        _require_exact_fields(
            envelope,
            {"data", "model", "version"},
            "payload",
        )

        version = _require_integer(envelope["version"], "payload.version")
        if version != SERIALIZATION_VERSION:
            raise ValueError(
                f"unsupported execution serialization version: {version}"
            )

        model_name = _require_string(envelope["model"], "payload.model")
        data = _require_object(envelope["data"], "payload.data")
        return _decode_model(model_name, data)
    except ExecutionSerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionSerializationError(
            f"invalid serialized execution payload: {exc}",
            operation="deserialize",
        ) from exc


def _encode_model(model: ExecutionModel) -> JsonObject:
    if isinstance(model, ExecutionLimits):
        return _encode_limits(model)
    if isinstance(model, ExecutionRequest):
        return {
            "argv": list(model.argv) if model.argv is not None else None,
            "backend": model.backend,
            "environment": [list(item) for item in model.environment],
            "filesystem_mode": model.filesystem_mode,
            "limits": _encode_limits(model.limits),
            "mode": model.mode,
            "network_access": model.network_access,
            "require_cancellation": model.require_cancellation,
            "script": model.script,
            "shell_kind": model.shell_kind,
            "working_directory": _encode_host_path(model.working_directory),
        }
    if isinstance(model, PolicyDecision):
        return {
            "allowed": model.allowed,
            "effective_limits": _encode_limits(model.effective_limits),
            "reason": model.reason,
        }
    if isinstance(model, BackendCapabilities):
        return {
            "cancellation": model.cancellation,
            "cpu_limits": model.cpu_limits,
            "filesystem_isolation": model.filesystem_isolation,
            "memory_limits": model.memory_limits,
            "network_isolation": model.network_isolation,
            "process_limits": model.process_limits,
            "supported_execution_modes": list(model.supported_execution_modes),
            "supported_filesystem_modes": list(model.supported_filesystem_modes),
            "supported_shells": list(model.supported_shells),
            "timeout_enforcement": model.timeout_enforcement,
        }
    if isinstance(model, CapabilityCheck):
        return {
            "compatible": model.compatible,
            "reasons": list(model.reasons),
        }
    if isinstance(model, ExecutionResult):
        return {
            "audit_id": model.audit_id,
            "backend": model.backend,
            "duration_seconds": model.duration_seconds,
            "exit_code": model.exit_code,
            "status": model.status,
            "stderr": model.stderr,
            "stderr_bytes": model.stderr_bytes,
            "stderr_truncated": model.stderr_truncated,
            "stdout": model.stdout,
            "stdout_bytes": model.stdout_bytes,
            "stdout_truncated": model.stdout_truncated,
            "termination_reason": model.termination_reason,
        }
    if isinstance(model, ExecutionContext):
        return {
            "execution_id": model.execution_id,
            "launched_at_utc": _encode_datetime(model.launched_at_utc),
            "project_root": _encode_host_path(model.project_root),
            "session_id": model.session_id,
            "tool_call_id": model.tool_call_id,
            "turn_id": model.turn_id,
            "workspace_id": model.workspace_id,
        }
    if isinstance(model, NativeDiagnostic):
        return {
            "code": model.code,
            "message": model.message,
            "platform": model.platform,
        }
    if isinstance(model, ExecutionLifecycleEvent):
        return {
            "details": [list(item) for item in model.details],
            "execution_id": model.execution_id,
            "message": model.message,
            "occurred_at_utc": _encode_datetime(model.occurred_at_utc),
            "sequence": model.sequence,
            "stage": model.stage,
        }

    raise TypeError(f"unsupported execution model: {type(model).__name__}")


def _decode_model(model_name: str, data: JsonObject) -> ExecutionModel:
    if model_name == "execution_limits":
        return _decode_limits(data)
    if model_name == "execution_request":
        _require_exact_fields(
            data,
            {
                "argv",
                "backend",
                "environment",
                "filesystem_mode",
                "limits",
                "mode",
                "network_access",
                "require_cancellation",
                "script",
                "shell_kind",
                "working_directory",
            },
            model_name,
        )
        return ExecutionRequest(
            mode=data["mode"],  # type: ignore[arg-type]
            argv=_decode_optional_strings(data["argv"], "argv"),
            script=_decode_optional_string(data["script"], "script"),
            working_directory=_decode_host_path(
                data["working_directory"],
                "working_directory",
            ),
            limits=_decode_limits(_require_object(data["limits"], "limits")),
            network_access=data["network_access"],  # type: ignore[arg-type]
            filesystem_mode=data["filesystem_mode"],  # type: ignore[arg-type]
            backend=data["backend"],  # type: ignore[arg-type]
            shell_kind=data["shell_kind"],  # type: ignore[arg-type]
            environment=_decode_string_pairs(data["environment"], "environment"),
            require_cancellation=data["require_cancellation"],  # type: ignore[arg-type]
        )
    if model_name == "policy_decision":
        _require_exact_fields(
            data,
            {"allowed", "effective_limits", "reason"},
            model_name,
        )
        return PolicyDecision(
            allowed=data["allowed"],  # type: ignore[arg-type]
            reason=_decode_optional_string(data["reason"], "reason"),
            effective_limits=_decode_limits(
                _require_object(
                    data["effective_limits"],
                    "effective_limits",
                )
            ),
        )
    if model_name == "backend_capabilities":
        _require_exact_fields(
            data,
            {
                "cancellation",
                "cpu_limits",
                "filesystem_isolation",
                "memory_limits",
                "network_isolation",
                "process_limits",
                "supported_execution_modes",
                "supported_filesystem_modes",
                "supported_shells",
                "timeout_enforcement",
            },
            model_name,
        )
        return BackendCapabilities(
            filesystem_isolation=data["filesystem_isolation"],  # type: ignore[arg-type]
            network_isolation=data["network_isolation"],  # type: ignore[arg-type]
            memory_limits=data["memory_limits"],  # type: ignore[arg-type]
            cpu_limits=data["cpu_limits"],  # type: ignore[arg-type]
            process_limits=data["process_limits"],  # type: ignore[arg-type]
            timeout_enforcement=data["timeout_enforcement"],  # type: ignore[arg-type]
            cancellation=data["cancellation"],  # type: ignore[arg-type]
            supported_execution_modes=_decode_strings(
                data["supported_execution_modes"],
                "supported_execution_modes",
            ),
            supported_filesystem_modes=_decode_strings(
                data["supported_filesystem_modes"],
                "supported_filesystem_modes",
            ),
            supported_shells=_decode_strings(
                data["supported_shells"],
                "supported_shells",
            ),
        )
    if model_name == "capability_check":
        _require_exact_fields(
            data,
            {"compatible", "reasons"},
            model_name,
        )
        return CapabilityCheck(
            compatible=data["compatible"],  # type: ignore[arg-type]
            reasons=_decode_strings(data["reasons"], "reasons"),
        )
    if model_name == "execution_result":
        _require_exact_fields(
            data,
            {
                "audit_id",
                "backend",
                "duration_seconds",
                "exit_code",
                "status",
                "stderr",
                "stderr_bytes",
                "stderr_truncated",
                "stdout",
                "stdout_bytes",
                "stdout_truncated",
                "termination_reason",
            },
            model_name,
        )
        return ExecutionResult(
            status=data["status"],  # type: ignore[arg-type]
            exit_code=data["exit_code"],  # type: ignore[arg-type]
            stdout=data["stdout"],  # type: ignore[arg-type]
            stderr=data["stderr"],  # type: ignore[arg-type]
            duration_seconds=data["duration_seconds"],  # type: ignore[arg-type]
            stdout_bytes=data["stdout_bytes"],  # type: ignore[arg-type]
            stderr_bytes=data["stderr_bytes"],  # type: ignore[arg-type]
            stdout_truncated=data["stdout_truncated"],  # type: ignore[arg-type]
            stderr_truncated=data["stderr_truncated"],  # type: ignore[arg-type]
            termination_reason=data["termination_reason"],  # type: ignore[arg-type]
            backend=data["backend"],  # type: ignore[arg-type]
            audit_id=data["audit_id"],  # type: ignore[arg-type]
        )
    if model_name == "execution_context":
        _require_exact_fields(
            data,
            {
                "execution_id",
                "launched_at_utc",
                "project_root",
                "session_id",
                "tool_call_id",
                "turn_id",
                "workspace_id",
            },
            model_name,
        )
        return ExecutionContext(
            execution_id=data["execution_id"],  # type: ignore[arg-type]
            tool_call_id=data["tool_call_id"],  # type: ignore[arg-type]
            session_id=data["session_id"],  # type: ignore[arg-type]
            turn_id=data["turn_id"],  # type: ignore[arg-type]
            workspace_id=data["workspace_id"],  # type: ignore[arg-type]
            project_root=_decode_host_path(data["project_root"], "project_root"),
            launched_at_utc=_decode_datetime(
                data["launched_at_utc"],
                "launched_at_utc",
            ),
        )
    if model_name == "native_diagnostic":
        _require_exact_fields(
            data,
            {"code", "message", "platform"},
            model_name,
        )
        return NativeDiagnostic(
            code=data["code"],  # type: ignore[arg-type]
            message=data["message"],  # type: ignore[arg-type]
            platform=data["platform"],  # type: ignore[arg-type]
        )
    if model_name == "execution_lifecycle_event":
        _require_exact_fields(
            data,
            {
                "details",
                "execution_id",
                "message",
                "occurred_at_utc",
                "sequence",
                "stage",
            },
            model_name,
        )
        return ExecutionLifecycleEvent(
            execution_id=data["execution_id"],  # type: ignore[arg-type]
            stage=data["stage"],  # type: ignore[arg-type]
            occurred_at_utc=_decode_datetime(
                data["occurred_at_utc"],
                "occurred_at_utc",
            ),
            sequence=data["sequence"],  # type: ignore[arg-type]
            message=_decode_optional_string(data["message"], "message"),
            details=_decode_string_pairs(data["details"], "details"),
        )

    raise ValueError(f"unknown execution model: {model_name!r}")


def _encode_limits(limits: ExecutionLimits) -> JsonObject:
    return {
        "cpu_seconds": limits.cpu_seconds,
        "max_output_bytes": limits.max_output_bytes,
        "max_processes": limits.max_processes,
        "max_return_bytes": limits.max_return_bytes,
        "memory_bytes": limits.memory_bytes,
        "termination_grace_seconds": limits.termination_grace_seconds,
        "timeout_seconds": limits.timeout_seconds,
    }


def _decode_limits(data: JsonObject) -> ExecutionLimits:
    _require_exact_fields(
        data,
        {
            "cpu_seconds",
            "max_output_bytes",
            "max_processes",
            "max_return_bytes",
            "memory_bytes",
            "termination_grace_seconds",
            "timeout_seconds",
        },
        "execution_limits",
    )
    return ExecutionLimits(
        timeout_seconds=data["timeout_seconds"],  # type: ignore[arg-type]
        max_output_bytes=data["max_output_bytes"],  # type: ignore[arg-type]
        max_return_bytes=data["max_return_bytes"],  # type: ignore[arg-type]
        memory_bytes=data["memory_bytes"],  # type: ignore[arg-type]
        cpu_seconds=data["cpu_seconds"],  # type: ignore[arg-type]
        max_processes=data["max_processes"],  # type: ignore[arg-type]
        termination_grace_seconds=data[  # type: ignore[arg-type]
            "termination_grace_seconds"
        ],
    )


def _encode_host_path(path: Path) -> JsonObject:
    return {
        "flavor": _NATIVE_PATH_FLAVOR,
        "value": str(path),
    }


def _decode_host_path(value: object, name: str) -> Path:
    data = _require_object(value, name)
    _require_exact_fields(data, {"flavor", "value"}, name)
    flavor = _require_string(data["flavor"], f"{name}.flavor")
    if flavor not in {"posix", "windows"}:
        raise ValueError(f"{name}.flavor is unknown: {flavor!r}")
    if flavor != _NATIVE_PATH_FLAVOR:
        raise ValueError(
            f"{name} is a {flavor} host path and cannot be restored on "
            f"a {_NATIVE_PATH_FLAVOR} host"
        )
    return Path(_require_string(data["value"], f"{name}.value"))


def _encode_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _decode_datetime(value: object, name: str) -> datetime:
    text = _require_string(value, name)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from exc


def _decode_optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, name)


def _decode_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return tuple(
        _require_string(item, f"{name}[{index}]")
        for index, item in enumerate(value)
    )


def _decode_optional_strings(
    value: object,
    name: str,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _decode_strings(value, name)


def _decode_string_pairs(
    value: object,
    name: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")

    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 2:
            raise TypeError(f"{name}[{index}] must be a two-item array")
        pairs.append(
            (
                _require_string(item[0], f"{name}[{index}][0]"),
                _require_string(item[1], f"{name}[{index}][1]"),
            )
        )
    return tuple(pairs)


def _require_object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _require_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_exact_fields(
    data: JsonObject,
    expected: set[str],
    name: str,
) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ValueError(f"{name} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(unknown)}")
