from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.execution.audit.retention import RetentionPolicy
from truecoder.execution.bootstrap import (
    ExecutionBootstrapConfig,
    default_policy_config,
)
from truecoder.execution.environment import EnvironmentPolicy
from truecoder.execution.models import ExecutionLimits, RiskLevel
from truecoder.execution.policy import PolicyConfig

EXECUTION_CONFIG_VERSION: Final = 1
MAX_CONFIG_BYTES: Final = 256 * 1024


class ExecutionConfigError(ValueError):
    pass


def default_execution_config_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "execution.json"


def load_execution_config(path: Path | None = None) -> ExecutionBootstrapConfig:
    target = path or default_execution_config_path()
    if not isinstance(target, Path):
        raise ExecutionConfigError("path must be a pathlib.Path")
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ExecutionBootstrapConfig()
    except (OSError, UnicodeDecodeError) as error:
        raise ExecutionConfigError(
            f"execution configuration could not be read: {error}"
        ) from None
    return parse_execution_config(raw, base_directory=target.parent)


def parse_execution_config(
    raw: str,
    *,
    base_directory: Path,
) -> ExecutionBootstrapConfig:
    if not isinstance(raw, str):
        raise ExecutionConfigError("execution configuration must be text")
    if not isinstance(base_directory, Path):
        raise ExecutionConfigError("base_directory must be a pathlib.Path")
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ExecutionConfigError("execution configuration is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ExecutionConfigError(
            f"execution configuration is not valid JSON: {error}"
        ) from None
    if not isinstance(payload, dict):
        raise ExecutionConfigError("execution configuration must be an object")
    _reject_unknown(
        payload,
        {
            "version",
            "enabled",
            "audit_database_path",
            "image_lock_path",
            "trusted_rules_path",
            "limits",
            "policy",
            "environment",
            "container",
            "retention",
        },
        "configuration",
    )
    version = payload.get("version", EXECUTION_CONFIG_VERSION)
    if version != EXECUTION_CONFIG_VERSION:
        raise ExecutionConfigError(
            f"unsupported execution configuration version: {version!r}"
        )

    defaults = ExecutionBootstrapConfig()
    limits = _limits(payload.get("limits"), defaults.policy_config.limit_ceiling)
    policy = _policy(payload.get("policy"), defaults.policy_config, limits)
    environment = _environment(
        payload.get("environment"),
        defaults.environment_policy,
    )
    container = _object(payload.get("container"), "container")
    _reject_unknown(
        container,
        {
            "default_memory_bytes",
            "default_pids_limit",
            "cpu_rate_ceiling",
            "isolated_network",
        },
        "container",
    )
    retention = _object(payload.get("retention"), "retention")
    _reject_unknown(retention, {"days"}, "retention")

    return ExecutionBootstrapConfig(
        enabled=_boolean(payload.get("enabled", defaults.enabled), "enabled"),
        audit_database_path=_path(
            payload.get("audit_database_path"),
            defaults.audit_database_path,
            base_directory,
            "audit_database_path",
        ),
        image_lock_path=_path(
            payload.get("image_lock_path"),
            defaults.image_lock_path,
            base_directory,
            "image_lock_path",
        ),
        trusted_rules_path=_path(
            payload.get("trusted_rules_path"),
            defaults.trusted_rules_path,
            base_directory,
            "trusted_rules_path",
        ),
        policy_config=policy,
        environment_policy=environment,
        container_default_memory_bytes=_positive_integer(
            container.get(
                "default_memory_bytes",
                defaults.container_default_memory_bytes,
            ),
            "container.default_memory_bytes",
        ),
        container_default_pids_limit=_positive_integer(
            container.get(
                "default_pids_limit",
                defaults.container_default_pids_limit,
            ),
            "container.default_pids_limit",
        ),
        container_cpu_rate_ceiling=_optional_positive_number(
            container.get(
                "cpu_rate_ceiling",
                defaults.container_cpu_rate_ceiling,
            ),
            "container.cpu_rate_ceiling",
        ),
        container_isolated_network=_optional_text(
            container.get(
                "isolated_network",
                defaults.container_isolated_network,
            ),
            "container.isolated_network",
        ),
        retention_policy=_retention_policy(
            retention,
            defaults.retention_policy,
        ),
    )


def _limits(value: object, defaults: ExecutionLimits) -> ExecutionLimits:
    values = _object(value, "limits")
    _reject_unknown(
        values,
        {
            "timeout_seconds",
            "max_output_bytes",
            "max_return_bytes",
            "memory_bytes",
            "cpu_seconds",
            "max_processes",
            "termination_grace_seconds",
        },
        "limits",
    )
    try:
        return ExecutionLimits(
            timeout_seconds=values.get(
                "timeout_seconds",
                defaults.timeout_seconds,
            ),
            max_output_bytes=values.get(
                "max_output_bytes",
                defaults.max_output_bytes,
            ),
            max_return_bytes=values.get(
                "max_return_bytes",
                defaults.max_return_bytes,
            ),
            memory_bytes=values.get("memory_bytes", defaults.memory_bytes),
            cpu_seconds=values.get("cpu_seconds", defaults.cpu_seconds),
            max_processes=values.get(
                "max_processes",
                defaults.max_processes,
            ),
            termination_grace_seconds=values.get(
                "termination_grace_seconds",
                defaults.termination_grace_seconds,
            ),
        )
    except (TypeError, ValueError) as error:
        raise ExecutionConfigError(f"invalid limits: {error}") from None


def _policy(
    value: object,
    defaults: PolicyConfig,
    limits: ExecutionLimits,
) -> PolicyConfig:
    values = _object(value, "policy")
    _reject_unknown(
        values,
        {
            "version",
            "minimum_isolation",
            "limit_enforcement",
            "unknown_risk",
        },
        "policy",
    )
    raw_risk = values.get("unknown_risk", defaults.unknown_risk.value)
    try:
        unknown_risk = RiskLevel(raw_risk)
    except (TypeError, ValueError):
        raise ExecutionConfigError(
            f"policy.unknown_risk is invalid: {raw_risk!r}"
        ) from None
    try:
        return PolicyConfig(
            version=values.get("version", default_policy_config().version),
            limit_ceiling=limits,
            minimum_isolation=values.get(
                "minimum_isolation",
                defaults.minimum_isolation,
            ),
            limit_enforcement=values.get(
                "limit_enforcement",
                defaults.limit_enforcement,
            ),
            unknown_risk=unknown_risk,
        )
    except (TypeError, ValueError) as error:
        raise ExecutionConfigError(f"invalid policy: {error}") from None


def _environment(
    value: object,
    defaults: EnvironmentPolicy,
) -> EnvironmentPolicy:
    values = _object(value, "environment")
    _reject_unknown(
        values,
        {
            "additional_inherited_names",
            "include_home_paths",
            "max_inherited_entries",
        },
        "environment",
    )
    raw_names = values.get(
        "additional_inherited_names",
        list(defaults.additional_inherited_names),
    )
    if not isinstance(raw_names, list) or any(
        not isinstance(name, str) for name in raw_names
    ):
        raise ExecutionConfigError(
            "environment.additional_inherited_names must be an array of strings"
        )
    try:
        return EnvironmentPolicy(
            additional_inherited_names=tuple(raw_names),
            include_home_paths=_boolean(
                values.get("include_home_paths", defaults.include_home_paths),
                "environment.include_home_paths",
            ),
            max_inherited_entries=_positive_integer(
                values.get(
                    "max_inherited_entries",
                    defaults.max_inherited_entries,
                ),
                "environment.max_inherited_entries",
            ),
        )
    except (TypeError, ValueError) as error:
        raise ExecutionConfigError(f"invalid environment policy: {error}") from None


def _object(value: object, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExecutionConfigError(f"{name} must be an object")
    return value


def _reject_unknown(values: dict, allowed: set[str], name: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ExecutionConfigError(f"{name} has unknown fields: {sorted(unknown)}")


def _path(
    value: object,
    default: Path,
    base_directory: Path,
    name: str,
) -> Path:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ExecutionConfigError(f"{name} must be a non-empty path string")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    return candidate.resolve(strict=False)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutionConfigError(f"{name} must be a boolean")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionConfigError(f"{name} must be a positive integer")
    return value


def _optional_positive_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ExecutionConfigError(f"{name} must be a positive number or null")
    return float(value)


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ExecutionConfigError(f"{name} must be non-empty text or null")
    return value.strip()


def _retention_policy(
    values: dict,
    defaults: RetentionPolicy,
) -> RetentionPolicy:
    try:
        return RetentionPolicy(
            days=_positive_integer(
                values.get("days", defaults.days),
                "retention.days",
            )
        )
    except ValueError as error:
        raise ExecutionConfigError(
            f"invalid retention policy: {error}"
        ) from None
