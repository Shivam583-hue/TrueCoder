from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from .environment import classify_secret_name
from .models import (
    CAPABILITY_REQUIREMENT_LEVELS,
    CapabilityRequirementLevel,
    CapabilityRequirements,
    ExecutionLimits,
    ExecutionRequest,
    PolicyDecision,
    PolicyReason,
    RiskLevel,
)

_RISK_RANK: Final = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

_REQUIREMENT_RANK: Final = {
    "none": 0,
    "best_effort": 1,
    "enforced": 2,
}

_REQUIREMENT_FIELDS: Final = (
    "filesystem_isolation",
    "network_isolation",
    "memory_limits",
    "cpu_limits",
    "process_limits",
    "timeout_enforcement",
    "cancellation",
)

_LOW_RISK_EXECUTABLES: Final = frozenset(
    {
        "cargo",
        "go",
        "mypy",
        "npm",
        "pnpm",
        "pyright",
        "pytest",
        "ruff",
        "tox",
        "uv",
        "yarn",
    }
)

_READ_ONLY_EXECUTABLES: Final = frozenset(
    {
        "cat",
        "dir",
        "find",
        "grep",
        "head",
        "ls",
        "pwd",
        "rg",
        "tail",
        "tree",
        "type",
        "wc",
    }
)

_PACKAGE_MANAGERS: Final = frozenset(
    {
        "apt",
        "apt-get",
        "brew",
        "choco",
        "dnf",
        "gem",
        "npm",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "uv",
        "winget",
        "yarn",
    }
)

_NETWORK_EXECUTABLES: Final = frozenset({"curl", "ftp", "scp", "sftp", "ssh", "wget"})

_PERMISSION_EXECUTABLES: Final = frozenset(
    {"chgrp", "chmod", "chown", "icacls", "setfacl", "takeown"}
)

_DELETE_EXECUTABLES: Final = frozenset({"del", "erase", "rd", "rm", "rmdir", "shred"})

_DENIED_EXECUTABLES: Final = frozenset(
    {
        "diskpart",
        "doas",
        "fdisk",
        "halt",
        "mkfs",
        "poweroff",
        "reboot",
        "shutdown",
        "su",
        "sudo",
    }
)

_EXECUTABLE_SUFFIXES: Final = (".exe", ".cmd", ".bat", ".com")
_RECURSIVE_DELETE_FLAGS: Final = frozenset({"-r", "-rf", "-fr", "--recursive", "/s"})
_FORCE_DELETE_FLAGS: Final = frozenset({"-f", "-rf", "-fr", "--force", "/q"})

_PIPE_TO_SHELL = re.compile(
    r"(?:curl|wget)\b[\s\S]*\|\s*(?:ba|z|fi)?sh\b",
    re.IGNORECASE,
)
_PRIVILEGED_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo|doas|su|mkfs|diskpart|shutdown|reboot)\b",
    re.IGNORECASE,
)
_RECURSIVE_DELETE_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|rmdir|del|erase)\b[^\n;&|]*(?:"
    r"-[a-z]*r[a-z]*|--recursive|/s)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    version: str
    limit_ceiling: ExecutionLimits
    minimum_isolation: CapabilityRequirementLevel = "enforced"
    limit_enforcement: CapabilityRequirementLevel = "enforced"
    unknown_risk: RiskLevel = RiskLevel.MEDIUM

    def __post_init__(self) -> None:
        if not isinstance(self.version, str):
            raise TypeError("version must be a string")
        if not self.version.strip():
            raise ValueError("version must not be empty")
        if "\x00" in self.version:
            raise ValueError("version must not contain null bytes")
        if not isinstance(self.limit_ceiling, ExecutionLimits):
            raise TypeError("limit_ceiling must be an ExecutionLimits")
        for name, value in (
            ("minimum_isolation", self.minimum_isolation),
            ("limit_enforcement", self.limit_enforcement),
        ):
            if value not in CAPABILITY_REQUIREMENT_LEVELS:
                raise ValueError(f"unknown {name}: {value!r}")
            if value == "none":
                raise ValueError(f"{name} must require a capability level")
        if not isinstance(self.unknown_risk, RiskLevel):
            raise TypeError("unknown_risk must be a RiskLevel")


@dataclass(frozen=True, slots=True)
class _Finding:
    reason: PolicyReason
    risk: RiskLevel
    requires_approval: bool = False
    deny: bool = False
    requirements: CapabilityRequirements = field(default_factory=CapabilityRequirements)


def tighten_limits(
    requested: ExecutionLimits,
    ceiling: ExecutionLimits,
) -> ExecutionLimits:
    """Return limits that are no weaker than either input."""

    if not isinstance(requested, ExecutionLimits):
        raise TypeError("requested must be an ExecutionLimits")
    if not isinstance(ceiling, ExecutionLimits):
        raise TypeError("ceiling must be an ExecutionLimits")

    max_output_bytes = min(
        requested.max_output_bytes,
        ceiling.max_output_bytes,
    )
    return ExecutionLimits(
        timeout_seconds=min(
            requested.timeout_seconds,
            ceiling.timeout_seconds,
        ),
        max_output_bytes=max_output_bytes,
        max_return_bytes=min(
            requested.max_return_bytes,
            ceiling.max_return_bytes,
            max_output_bytes,
        ),
        memory_bytes=_stricter_optional(
            requested.memory_bytes,
            ceiling.memory_bytes,
        ),
        cpu_seconds=_stricter_optional(
            requested.cpu_seconds,
            ceiling.cpu_seconds,
        ),
        max_processes=_stricter_optional(
            requested.max_processes,
            ceiling.max_processes,
        ),
        termination_grace_seconds=min(
            requested.termination_grace_seconds,
            ceiling.termination_grace_seconds,
        ),
    )


def evaluate_policy(
    request: ExecutionRequest,
    config: PolicyConfig,
) -> PolicyDecision:
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    if not isinstance(config, PolicyConfig):
        raise TypeError("config must be a PolicyConfig")

    findings = [
        *_environment_findings(request),
        *_request_shape_findings(request),
        *(
            _shell_findings(request.script or "")
            if request.mode == "shell"
            else _exec_findings(request.argv or (), config)
        ),
    ]
    requirements = _base_requirements(request, config)
    for finding in findings:
        requirements = merge_requirements(
            requirements,
            finding.requirements,
        )

    risk = max(
        (finding.risk for finding in findings),
        key=_RISK_RANK.__getitem__,
        default=RiskLevel.LOW,
    )
    denied = any(finding.deny for finding in findings)
    reasons = _unique_reasons(findings)
    return PolicyDecision(
        allowed=not denied,
        risk=risk,
        requires_approval=(
            not denied
            and (
                risk is not RiskLevel.LOW
                or any(finding.requires_approval for finding in findings)
            )
        ),
        effective_limits=tighten_limits(
            request.limits,
            config.limit_ceiling,
        ),
        requirements=requirements,
        reasons=reasons,
    )


def merge_requirements(
    first: CapabilityRequirements,
    second: CapabilityRequirements,
) -> CapabilityRequirements:
    if not isinstance(first, CapabilityRequirements):
        raise TypeError("first must be CapabilityRequirements")
    if not isinstance(second, CapabilityRequirements):
        raise TypeError("second must be CapabilityRequirements")

    values = {}
    for field_name in _REQUIREMENT_FIELDS:
        left = getattr(first, field_name)
        right = getattr(second, field_name)
        values[field_name] = (
            left if _REQUIREMENT_RANK[left] >= _REQUIREMENT_RANK[right] else right
        )
    return CapabilityRequirements(**values)


def portable_executable_name(argv_zero: str) -> str:
    if not isinstance(argv_zero, str):
        raise TypeError("argv_zero must be a string")
    if not argv_zero.strip():
        raise ValueError("argv_zero must not be empty")

    name = argv_zero.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _request_shape_findings(request: ExecutionRequest) -> tuple[_Finding, ...]:
    findings: list[_Finding] = []
    if request.mode == "shell":
        findings.append(
            _finding(
                "shell-script",
                "policy.020.shell-script",
                "Shell-script mode can combine and conceal multiple operations.",
                RiskLevel.HIGH,
                approval=True,
            )
        )
    if request.filesystem_mode == "host":
        findings.append(
            _finding(
                "host-filesystem",
                "policy.030.host-filesystem",
                "The request asks for unrestricted host filesystem access.",
                RiskLevel.HIGH,
                approval=True,
            )
        )
    elif request.filesystem_mode == "workspace-write":
        findings.append(
            _finding(
                "workspace-write",
                "policy.031.workspace-write",
                "The command may modify the workspace.",
                RiskLevel.MEDIUM,
                approval=True,
            )
        )
    if request.network_access:
        findings.append(
            _finding(
                "network-enabled",
                "policy.040.network-enabled",
                "The command may access external networks.",
                RiskLevel.MEDIUM,
                approval=True,
            )
        )
    return tuple(findings)


def _environment_findings(request: ExecutionRequest) -> tuple[_Finding, ...]:
    findings: list[_Finding] = []
    for name, _value in request.environment:
        match = classify_secret_name(name)
        if not match.sensitive:
            continue
        findings.append(
            _finding(
                f"sensitive-environment-{match.category}",
                f"policy.010.environment.{match.rule_id}",
                f"Environment variable {name!r} is classified as sensitive.",
                RiskLevel.CRITICAL,
                deny=True,
            )
        )
    return tuple(findings)


def _shell_findings(script: str) -> tuple[_Finding, ...]:
    findings: list[_Finding] = []
    if _PIPE_TO_SHELL.search(script):
        findings.append(
            _finding(
                "download-piped-to-shell",
                "policy.011.download-piped-to-shell",
                "Downloaded content is piped directly into a shell.",
                RiskLevel.CRITICAL,
                deny=True,
            )
        )
    if _PRIVILEGED_SHELL.search(script):
        findings.append(
            _finding(
                "privileged-shell-command",
                "policy.012.privileged-shell-command",
                "The shell script invokes a privileged or system command.",
                RiskLevel.CRITICAL,
                deny=True,
            )
        )
    if _RECURSIVE_DELETE_SHELL.search(script):
        findings.append(
            _finding(
                "recursive-deletion",
                "policy.013.recursive-deletion",
                "The shell script appears to delete entries recursively.",
                RiskLevel.HIGH,
                approval=True,
            )
        )
    return tuple(findings)


def _exec_findings(
    argv: tuple[str, ...],
    config: PolicyConfig,
) -> tuple[_Finding, ...]:
    executable = portable_executable_name(argv[0])
    arguments = tuple(argument.casefold() for argument in argv[1:])

    if executable in _DENIED_EXECUTABLES:
        return (
            _finding(
                "privileged-system-command",
                "policy.011.privileged-system-command",
                f"{executable!r} is a privileged or system-level command.",
                RiskLevel.CRITICAL,
                deny=True,
            ),
        )
    if executable == "git":
        return _git_findings(arguments)
    if executable in _DELETE_EXECUTABLES:
        return _delete_findings(executable, arguments)
    if executable in _PERMISSION_EXECUTABLES:
        return (
            _finding(
                "permission-mutation",
                "policy.060.permission-mutation",
                f"{executable!r} changes filesystem ownership or permissions.",
                RiskLevel.HIGH,
                approval=True,
            ),
        )
    if executable in _NETWORK_EXECUTABLES:
        return (
            _finding(
                "network-utility",
                "policy.070.network-utility",
                f"{executable!r} is a network-capable utility.",
                RiskLevel.HIGH,
                approval=True,
            ),
        )
    if _is_python_test_command(executable, arguments):
        return (_known_command_finding(executable, "test"),)
    if executable in _LOW_RISK_EXECUTABLES:
        if _is_package_install(executable, arguments):
            return (
                _finding(
                    "package-installation",
                    "policy.080.package-installation",
                    f"{executable!r} may install or change dependencies.",
                    RiskLevel.MEDIUM,
                    approval=True,
                ),
            )
        return (_known_command_finding(executable, "build or test"),)
    if executable in _READ_ONLY_EXECUTABLES:
        return (_known_command_finding(executable, "read-only"),)
    if executable in _PACKAGE_MANAGERS:
        return (
            _finding(
                "package-manager",
                "policy.081.package-manager",
                f"{executable!r} may mutate the development environment.",
                RiskLevel.MEDIUM,
                approval=True,
            ),
        )
    return (
        _finding(
            "unknown-command",
            "policy.900.unknown-command",
            f"No trusted policy rule matches executable {executable!r}.",
            config.unknown_risk,
            approval=True,
        ),
    )


def _git_findings(arguments: tuple[str, ...]) -> tuple[_Finding, ...]:
    subcommand = next(
        (argument for argument in arguments if not argument.startswith("-")),
        "",
    )
    if subcommand in {"status", "diff", "log", "show"}:
        return (_known_command_finding("git", f"read-only {subcommand}"),)
    if subcommand == "reset" and "--hard" in arguments:
        return (
            _finding(
                "destructive-git-reset",
                "policy.050.destructive-git-reset",
                "git reset --hard can discard uncommitted work.",
                RiskLevel.HIGH,
                approval=True,
            ),
        )
    if subcommand == "clean" and any(
        flag == "-f" or (flag.startswith("-") and "f" in flag) for flag in arguments
    ):
        return (
            _finding(
                "destructive-git-clean",
                "policy.051.destructive-git-clean",
                "Forced git clean deletes untracked workspace files.",
                RiskLevel.HIGH,
                approval=True,
            ),
        )
    if subcommand in {"clone", "fetch", "pull", "push"}:
        return (
            _finding(
                "git-network-mutation",
                "policy.052.git-network-mutation",
                f"git {subcommand} communicates with a remote repository.",
                RiskLevel.MEDIUM,
                approval=True,
            ),
        )
    return (
        _finding(
            "git-mutation",
            "policy.053.git-mutation",
            f"git {subcommand or 'operation'} may change repository state.",
            RiskLevel.MEDIUM,
            approval=True,
        ),
    )


def _delete_findings(
    executable: str,
    arguments: tuple[str, ...],
) -> tuple[_Finding, ...]:
    recursive = any(flag in _RECURSIVE_DELETE_FLAGS for flag in arguments)
    forced = any(flag in _FORCE_DELETE_FLAGS for flag in arguments)
    if recursive or forced:
        return (
            _finding(
                "recursive-or-forced-deletion",
                "policy.054.recursive-or-forced-deletion",
                f"{executable!r} requests recursive or forced deletion.",
                RiskLevel.HIGH,
                approval=True,
            ),
        )
    return (
        _finding(
            "filesystem-deletion",
            "policy.055.filesystem-deletion",
            f"{executable!r} deletes filesystem entries.",
            RiskLevel.MEDIUM,
            approval=True,
        ),
    )


def _known_command_finding(executable: str, category: str) -> _Finding:
    return _finding(
        "known-command",
        "policy.100.known-command",
        f"{executable!r} matches a known {category} command rule.",
        RiskLevel.LOW,
    )


def _is_python_test_command(
    executable: str,
    arguments: tuple[str, ...],
) -> bool:
    return executable in {"python", "python3", "py"} and any(
        arguments[index : index + 2] == ("-m", module)
        for module in ("pytest", "unittest")
        for index in range(max(0, len(arguments) - 1))
    )


def _is_package_install(
    executable: str,
    arguments: tuple[str, ...],
) -> bool:
    if executable in {"npm", "pnpm", "yarn"}:
        return any(
            argument in {"add", "install", "update", "upgrade"}
            for argument in arguments
        )
    if executable == "uv":
        return any(argument in {"add", "remove", "sync"} for argument in arguments)
    return False


def _base_requirements(
    request: ExecutionRequest,
    config: PolicyConfig,
) -> CapabilityRequirements:
    return CapabilityRequirements(
        filesystem_isolation=(
            config.minimum_isolation if request.filesystem_mode != "host" else "none"
        ),
        network_isolation=(
            config.minimum_isolation if not request.network_access else "none"
        ),
        memory_limits=(
            config.limit_enforcement
            if request.limits.memory_bytes is not None
            else "none"
        ),
        cpu_limits=(
            config.limit_enforcement
            if request.limits.cpu_seconds is not None
            else "none"
        ),
        process_limits=(
            config.limit_enforcement
            if request.limits.max_processes is not None
            else "none"
        ),
        timeout_enforcement=config.limit_enforcement,
        cancellation=(
            config.limit_enforcement if request.require_cancellation else "none"
        ),
    )


def _unique_reasons(findings: list[_Finding]) -> tuple[PolicyReason, ...]:
    reasons: list[PolicyReason] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.reason.code in seen:
            continue
        reasons.append(finding.reason)
        seen.add(finding.reason.code)
    return tuple(reasons)


def _finding(
    code: str,
    rule_id: str,
    message: str,
    risk: RiskLevel,
    *,
    approval: bool = False,
    deny: bool = False,
    requirements: CapabilityRequirements | None = None,
) -> _Finding:
    return _Finding(
        reason=PolicyReason(code=code, message=message, rule_id=rule_id),
        risk=risk,
        requires_approval=approval,
        deny=deny,
        requirements=requirements or CapabilityRequirements(),
    )


def _stricter_optional(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)
