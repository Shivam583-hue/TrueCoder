from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from truecoder.execution.errors import ApprovalError
from truecoder.execution.models import (
    BACKEND_NAMES,
    BackendCapabilities,
    BackendName,
    ExecutionLimits,
    ExecutionRequest,
)

_FINGERPRINT_VERSION = 1


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalScope(str, Enum):
    ONCE = "once"
    SESSION = "session"
    WORKSPACE = "workspace"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ApprovalIdentity:
    session_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        _require_identity(self.session_id, "session_id")
        _require_identity(self.workspace_id, "workspace_id")


@dataclass(frozen=True, slots=True)
class ExecutionApprovalDetails:
    """The complete effective security contract shown for one execution."""

    execution_id: str
    command_display: str
    request: ExecutionRequest
    backend: BackendName
    capabilities: BackendCapabilities
    risk: RiskLevel
    reasons: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        _require_identity(self.execution_id, "execution_id")
        _require_identity(self.command_display, "command_display")
        _require_identity(self.policy_version, "policy_version")
        if not isinstance(self.request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest")
        if self.backend not in BACKEND_NAMES:
            raise ValueError(f"unknown backend: {self.backend!r}")
        if not isinstance(self.capabilities, BackendCapabilities):
            raise TypeError("capabilities must be BackendCapabilities")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("risk must be a RiskLevel")
        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")
        for index, reason in enumerate(self.reasons):
            _require_identity(reason, f"reasons[{index}]")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("reasons cannot contain duplicates")

    @property
    def effective_limits(self) -> ExecutionLimits:
        return self.request.limits

    @property
    def working_directory(self) -> Path:
        return self.request.working_directory


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    call_id: str
    tool_name: str
    arguments_json: str
    identity: ApprovalIdentity
    fingerprint: str
    allowed_scopes: tuple[ApprovalScope, ...]
    execution: ExecutionApprovalDetails | None = None

    def __post_init__(self) -> None:
        _require_identity(self.call_id, "call_id")
        _require_identity(self.tool_name, "tool_name")
        _require_identity(self.fingerprint, "fingerprint")
        if not isinstance(self.identity, ApprovalIdentity):
            raise TypeError("identity must be an ApprovalIdentity")
        if not isinstance(self.arguments_json, str):
            raise TypeError("arguments_json must be a string")
        arguments = _decode_arguments(self.arguments_json)
        if _canonical_json(arguments) != self.arguments_json:
            raise ValueError("arguments_json must use canonical JSON encoding")
        _validate_scopes(self.allowed_scopes)
        if self.execution is not None and not isinstance(
            self.execution,
            ExecutionApprovalDetails,
        ):
            raise TypeError("execution must be ExecutionApprovalDetails or None")

    @classmethod
    def create(
        cls,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        identity: ApprovalIdentity,
        execution: ExecutionApprovalDetails | None = None,
        requested_scopes: tuple[ApprovalScope, ...] | None = None,
    ) -> ApprovalRequest:
        if not isinstance(identity, ApprovalIdentity):
            raise TypeError("identity must be an ApprovalIdentity")
        if execution is not None and not isinstance(
            execution,
            ExecutionApprovalDetails,
        ):
            raise TypeError("execution must be ExecutionApprovalDetails or None")
        arguments_json = _canonical_json(arguments)
        safe_scopes = safe_approval_scopes(execution, tool_name=tool_name)
        allowed_scopes = _restrict_scopes(requested_scopes, safe_scopes)
        fingerprint = build_approval_fingerprint(
            tool_name=tool_name,
            arguments_json=arguments_json,
            workspace_id=identity.workspace_id,
            execution=execution,
        )
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            arguments_json=arguments_json,
            identity=identity,
            fingerprint=fingerprint,
            allowed_scopes=allowed_scopes,
            execution=execution,
        )

    @property
    def arguments(self) -> dict[str, Any]:
        """Return a fresh copy of the validated display arguments."""

        return _decode_arguments(self.arguments_json)


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    decision: ApprovalDecision
    scope: ApprovalScope | None = None
    reused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ApprovalDecision):
            raise TypeError("decision must be an ApprovalDecision")
        if self.scope is not None and not isinstance(self.scope, ApprovalScope):
            raise TypeError("scope must be an ApprovalScope or None")
        if not isinstance(self.reused, bool):
            raise TypeError("reused must be a boolean")
        if self.decision is ApprovalDecision.APPROVED and self.scope is None:
            raise ValueError("approved responses require a scope")
        if self.decision is ApprovalDecision.REJECTED:
            if self.scope is not None:
                raise ValueError("rejected responses cannot have a scope")
            if self.reused:
                raise ValueError("rejected responses cannot be reused")
        if self.reused and self.scope is ApprovalScope.ONCE:
            raise ValueError("approve-once responses cannot be reused")

    @classmethod
    def approve(
        cls,
        scope: ApprovalScope = ApprovalScope.ONCE,
        *,
        reused: bool = False,
    ) -> ApprovalResponse:
        return cls(ApprovalDecision.APPROVED, scope, reused)

    @classmethod
    def reject(cls) -> ApprovalResponse:
        return cls(ApprovalDecision.REJECTED)


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]


class ApprovalGrantStore:
    """Store exact in-process grants without retaining command contents."""

    def __init__(self) -> None:
        self._session_grants: set[tuple[str, str]] = set()
        self._workspace_grants: set[tuple[str, str]] = set()

    def find(self, request: ApprovalRequest) -> ApprovalScope | None:
        session_key = (request.identity.session_id, request.fingerprint)
        if session_key in self._session_grants:
            return ApprovalScope.SESSION

        workspace_key = (request.identity.workspace_id, request.fingerprint)
        if workspace_key in self._workspace_grants:
            return ApprovalScope.WORKSPACE
        return None

    def remember(self, request: ApprovalRequest, scope: ApprovalScope) -> None:
        if not isinstance(scope, ApprovalScope):
            raise TypeError("scope must be an ApprovalScope")
        if scope not in request.allowed_scopes:
            raise ApprovalError(
                f"approval scope {scope.value!r} is not allowed for this request",
                operation="remember_approval",
            )
        if scope is ApprovalScope.ONCE:
            return
        if scope is ApprovalScope.SESSION:
            self._session_grants.add(
                (request.identity.session_id, request.fingerprint)
            )
            return
        self._workspace_grants.add(
            (request.identity.workspace_id, request.fingerprint)
        )


class ApprovalService:
    """Resolve exact reusable grants before asking the user."""

    def __init__(
        self,
        handler: ApprovalHandler,
        store: ApprovalGrantStore | None = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handler = handler
        self._store = store or ApprovalGrantStore()

    @property
    def handler(self) -> ApprovalHandler:
        return self._handler

    @handler.setter
    def handler(self, value: ApprovalHandler) -> None:
        if not callable(value):
            raise TypeError("handler must be callable")
        self._handler = value

    async def authorize(self, request: ApprovalRequest) -> ApprovalResponse:
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")

        reused = self.reusable_response(request)
        if reused is not None:
            return reused

        response = await self._handler(request)
        if not isinstance(response, ApprovalResponse):
            raise ApprovalError(
                "approval handler returned an invalid response",
                operation="request_approval",
            )
        if (
            response.decision is ApprovalDecision.APPROVED
            and response.scope not in request.allowed_scopes
        ):
            raise ApprovalError(
                "approval handler selected a scope that is unsafe for this request",
                operation="request_approval",
            )
        if response.reused:
            raise ApprovalError(
                "approval handlers cannot mark responses as reused",
                operation="request_approval",
            )

        if response.scope is not None:
            self._store.remember(request, response.scope)
        return response

    def reusable_response(
        self,
        request: ApprovalRequest,
    ) -> ApprovalResponse | None:
        """Return an exact reusable grant without invoking the UI handler."""

        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")
        reused_scope = self._store.find(request)
        if reused_scope is None or reused_scope not in request.allowed_scopes:
            return None
        return ApprovalResponse.approve(reused_scope, reused=True)


def safe_approval_scopes(
    execution: ExecutionApprovalDetails | None,
    *,
    tool_name: str | None = None,
) -> tuple[ApprovalScope, ...]:
    """Return the maximum scopes the UI may offer for this operation."""

    if tool_name == "shell":
        return (ApprovalScope.ONCE,)
    if execution is None:
        return (
            ApprovalScope.ONCE,
            ApprovalScope.SESSION,
            ApprovalScope.WORKSPACE,
        )
    if execution.request.mode == "shell" or execution.risk is not RiskLevel.LOW:
        return (ApprovalScope.ONCE,)
    return (
        ApprovalScope.ONCE,
        ApprovalScope.SESSION,
        ApprovalScope.WORKSPACE,
    )


def build_approval_fingerprint(
    *,
    tool_name: str,
    arguments_json: str,
    workspace_id: str,
    execution: ExecutionApprovalDetails | None = None,
) -> str:
    """Hash every security-relevant input while excluding transient IDs."""

    _require_identity(tool_name, "tool_name")
    _require_identity(workspace_id, "workspace_id")
    arguments = _decode_arguments(arguments_json)
    payload = {
        "arguments": arguments,
        "execution": (
            _execution_fingerprint_payload(execution)
            if execution is not None
            else None
        ),
        "tool_name": tool_name,
        "version": _FINGERPRINT_VERSION,
        "workspace_id": workspace_id,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"approval_{digest}"


async def reject_all_tool_calls(_request: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse.reject()


async def approve_all_tool_calls(_request: ApprovalRequest) -> ApprovalResponse:
    """Compatibility helper that approves each individual request once."""

    return ApprovalResponse.approve()


def _restrict_scopes(
    requested: tuple[ApprovalScope, ...] | None,
    safe: tuple[ApprovalScope, ...],
) -> tuple[ApprovalScope, ...]:
    if requested is None:
        return safe
    _validate_scopes(requested)
    restricted = tuple(scope for scope in requested if scope in safe)
    if ApprovalScope.ONCE not in restricted:
        restricted = (ApprovalScope.ONCE, *restricted)
    return restricted


def _validate_scopes(scopes: object) -> tuple[ApprovalScope, ...]:
    if not isinstance(scopes, tuple):
        raise TypeError("allowed_scopes must be a tuple")
    if not scopes:
        raise ValueError("allowed_scopes cannot be empty")
    if any(not isinstance(scope, ApprovalScope) for scope in scopes):
        raise TypeError("allowed_scopes must contain ApprovalScope values")
    if len(scopes) != len(set(scopes)):
        raise ValueError("allowed_scopes cannot contain duplicates")
    if ApprovalScope.ONCE not in scopes:
        raise ValueError("allowed_scopes must include approve-once")
    return scopes


def _execution_fingerprint_payload(
    details: ExecutionApprovalDetails,
) -> dict[str, Any]:
    request = details.request
    capabilities = details.capabilities
    return {
        "backend": details.backend,
        "capabilities": {
            "cancellation": capabilities.cancellation,
            "cpu_limits": capabilities.cpu_limits,
            "filesystem_isolation": capabilities.filesystem_isolation,
            "memory_limits": capabilities.memory_limits,
            "network_isolation": capabilities.network_isolation,
            "process_limits": capabilities.process_limits,
            "supported_execution_modes": capabilities.supported_execution_modes,
            "supported_filesystem_modes": capabilities.supported_filesystem_modes,
            "supported_shells": capabilities.supported_shells,
            "timeout_enforcement": capabilities.timeout_enforcement,
        },
        "command_display": details.command_display,
        "filesystem_mode": request.filesystem_mode,
        "environment": request.environment,
        "limits": {
            "cpu_seconds": request.limits.cpu_seconds,
            "max_output_bytes": request.limits.max_output_bytes,
            "max_processes": request.limits.max_processes,
            "max_return_bytes": request.limits.max_return_bytes,
            "memory_bytes": request.limits.memory_bytes,
            "termination_grace_seconds": (
                request.limits.termination_grace_seconds
            ),
            "timeout_seconds": request.limits.timeout_seconds,
        },
        "mode": request.mode,
        "network_access": request.network_access,
        "argv": request.argv,
        "backend_preference": request.backend,
        "policy_version": details.policy_version,
        "reasons": details.reasons,
        "require_cancellation": request.require_cancellation,
        "risk": details.risk.value,
        "script": request.script,
        "shell_kind": request.shell_kind,
        "working_directory": str(request.working_directory),
    }


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("approval data must be JSON serializable") from error


def _decode_arguments(arguments_json: str) -> dict[str, Any]:
    if not isinstance(arguments_json, str):
        raise TypeError("arguments_json must be a string")
    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError as error:
        raise ValueError("arguments_json must contain valid JSON") from error
    if not isinstance(arguments, dict):
        raise TypeError("arguments_json must contain a JSON object")
    return arguments


def _require_identity(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized
