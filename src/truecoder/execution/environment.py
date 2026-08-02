from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .models import (
    EXECUTION_PLATFORMS,
    MAX_ENVIRONMENT_BYTES,
    MAX_ENVIRONMENT_ENTRIES,
    MAX_ENVIRONMENT_NAME_BYTES,
    MAX_ENVIRONMENT_VALUE_BYTES,
    ExecutionPlatform,
    normalize_environment_name,
)

REDACTED_VALUE: Final = "<redacted>"
MAX_INHERITED_ENVIRONMENT_ENTRIES: Final = 4096
MAX_INHERITED_ENVIRONMENT_BYTES: Final = 1024 * 1024
MAX_REDACTION_VALUE_BYTES: Final = 4096
MIN_REDACTION_VALUE_CHARS: Final = 4

_POSIX_INHERITED: Final = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TERM",
    "TMPDIR",
    "TZ",
)
_WINDOWS_INHERITED: Final = (
    "ComSpec",
    "Path",
    "PATHEXT",
    "SystemRoot",
    "TEMP",
    "TMP",
)
_POSIX_HOME_NAMES: Final = ("HOME",)
_WINDOWS_HOME_NAMES: Final = (
    "APPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "USERPROFILE",
)

_SECRET_EXACT: Final = {
    "DATABASE_URL": "credential",
    "DOCKER_AUTH_CONFIG": "credential",
    "GITHUB_TOKEN": "token",
    "KUBECONFIG": "credential",
    "NPM_TOKEN": "token",
    "PYPI_TOKEN": "token",
    "SSH_AUTH_SOCK": "credential",
}
_SECRET_PREFIXES: Final = (
    ("ANTHROPIC_", "credential"),
    ("AWS_", "cloud"),
    ("AZURE_", "cloud"),
    ("GCP_", "cloud"),
    ("GOOGLE_", "cloud"),
    ("OPENAI_", "credential"),
)
_SECRET_SUFFIXES: Final = (
    ("_API_KEY", "api-key"),
    ("_CREDENTIAL", "credential"),
    ("_CREDENTIALS", "credential"),
    ("_PASSWORD", "password"),
    ("_PRIVATE_KEY", "private-key"),
    ("_SECRET", "secret"),
    ("_TOKEN", "token"),
)


@dataclass(frozen=True, slots=True)
class SecretNameMatch:
    sensitive: bool
    category: str
    rule_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.sensitive, bool):
            raise TypeError("sensitive must be a boolean")
        for name, value in (
            ("category", self.category),
            ("rule_id", self.rule_id),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if not self.sensitive and (self.category != "none" or self.rule_id != "none"):
            raise ValueError("non-sensitive matches must use none metadata")


@dataclass(frozen=True, slots=True)
class RemovedEnvironmentVariable:
    name: str
    reason_code: str

    def __post_init__(self) -> None:
        _required_text(self.name, "name")
        _required_text(self.reason_code, "reason_code")


@dataclass(frozen=True, slots=True)
class EnvironmentViolation:
    code: str
    name: str
    message: str

    def __post_init__(self) -> None:
        _required_text(self.code, "code")
        _required_text(self.name, "name")
        _required_text(self.message, "message")


@dataclass(frozen=True, slots=True)
class EnvironmentMetadata:
    inherited_names: tuple[str, ...]
    requested_names: tuple[str, ...]
    defined_names: tuple[str, ...]
    included_names: tuple[str, ...]
    removed: tuple[RemovedEnvironmentVariable, ...]
    overridden_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name, values in (
            ("inherited_names", self.inherited_names),
            ("requested_names", self.requested_names),
            ("defined_names", self.defined_names),
            ("included_names", self.included_names),
            ("overridden_names", self.overridden_names),
        ):
            _validate_names(values, field_name)
        if not isinstance(self.removed, tuple):
            raise TypeError("removed must be a tuple")
        if any(
            not isinstance(item, RemovedEnvironmentVariable) for item in self.removed
        ):
            raise TypeError("removed must contain RemovedEnvironmentVariable values")


@dataclass(frozen=True, slots=True)
class ConstructedEnvironment:
    variables: tuple[tuple[str, str], ...] = field(repr=False)
    redaction_values: tuple[str, ...] = field(repr=False)
    metadata: EnvironmentMetadata
    violations: tuple[EnvironmentViolation, ...]

    def __post_init__(self) -> None:
        _validate_pairs(self.variables, "variables")
        if not isinstance(self.redaction_values, tuple) or any(
            not isinstance(value, str) for value in self.redaction_values
        ):
            raise TypeError("redaction_values must be a tuple of strings")
        if not isinstance(self.metadata, EnvironmentMetadata):
            raise TypeError("metadata must be EnvironmentMetadata")
        if not isinstance(self.violations, tuple) or any(
            not isinstance(item, EnvironmentViolation) for item in self.violations
        ):
            raise TypeError("violations must contain EnvironmentViolation values")

    @property
    def valid(self) -> bool:
        return not self.violations


@dataclass(frozen=True, slots=True)
class EnvironmentPolicy:
    additional_inherited_names: tuple[str, ...] = ()
    include_home_paths: bool = False
    max_inherited_entries: int = MAX_INHERITED_ENVIRONMENT_ENTRIES

    def __post_init__(self) -> None:
        _validate_names(
            self.additional_inherited_names,
            "additional_inherited_names",
        )
        if not isinstance(self.include_home_paths, bool):
            raise TypeError("include_home_paths must be a boolean")
        if isinstance(self.max_inherited_entries, bool) or not isinstance(
            self.max_inherited_entries, int
        ):
            raise TypeError("max_inherited_entries must be an integer")
        if self.max_inherited_entries <= 0:
            raise ValueError("max_inherited_entries must be greater than zero")


def classify_secret_name(name: str) -> SecretNameMatch:
    text = _required_text(name, "name")
    normalized = text.casefold().upper()
    exact_category = _SECRET_EXACT.get(normalized)
    if exact_category is not None:
        return SecretNameMatch(
            sensitive=True,
            category=exact_category,
            rule_id=f"exact-{normalized.casefold().replace('_', '-')}",
        )
    for prefix, category in _SECRET_PREFIXES:
        if normalized.startswith(prefix):
            return SecretNameMatch(
                sensitive=True,
                category=category,
                rule_id=f"prefix-{prefix.casefold().strip('_')}",
            )
    for suffix, category in _SECRET_SUFFIXES:
        if normalized.endswith(suffix):
            return SecretNameMatch(
                sensitive=True,
                category=category,
                rule_id=f"suffix-{suffix.casefold().strip('_').replace('_', '-')}",
            )
    return SecretNameMatch(sensitive=False, category="none", rule_id="none")


def construct_environment(
    *,
    platform: ExecutionPlatform,
    inherited: Mapping[str, str],
    requested: tuple[tuple[str, str], ...],
    defined: tuple[tuple[str, str], ...] = (),
    policy: EnvironmentPolicy | None = None,
) -> ConstructedEnvironment:
    if platform not in EXECUTION_PLATFORMS:
        raise ValueError(f"unknown execution platform: {platform!r}")
    if not isinstance(inherited, Mapping):
        raise TypeError("inherited must be a mapping")
    effective_policy = policy or EnvironmentPolicy()
    if not isinstance(effective_policy, EnvironmentPolicy):
        raise TypeError("policy must be an EnvironmentPolicy")
    if len(inherited) > effective_policy.max_inherited_entries:
        raise ValueError("inherited environment exceeds the configured entry limit")

    inherited_pairs = tuple(inherited.items())
    _validate_pairs(
        inherited_pairs,
        "inherited",
        max_entries=effective_policy.max_inherited_entries,
        max_bytes=MAX_INHERITED_ENVIRONMENT_BYTES,
    )
    _validate_pairs(requested, "requested")
    _validate_pairs(defined, "defined")
    _validate_platform_unique(inherited_pairs, platform, "inherited")
    _validate_platform_unique(requested, platform, "requested")
    _validate_platform_unique(defined, platform, "defined")

    allowed_names = _allowed_inherited_names(platform, effective_policy)
    selected: dict[str, tuple[str, str]] = {}
    removed: list[RemovedEnvironmentVariable] = []
    violations: list[EnvironmentViolation] = []
    overridden_names: list[str] = []
    redaction_values: list[str] = []

    for name, value in sorted(
        inherited_pairs,
        key=lambda item: (normalize_environment_name(item[0], platform), item[0]),
    ):
        normalized = normalize_environment_name(name, platform)
        match = classify_secret_name(name)
        if match.sensitive:
            removed.append(
                RemovedEnvironmentVariable(
                    name=name,
                    reason_code=f"sensitive-{match.category}",
                )
            )
            _remember_redaction_value(redaction_values, value)
            continue
        if normalized not in allowed_names:
            removed.append(
                RemovedEnvironmentVariable(
                    name=name,
                    reason_code="not-in-minimal-allowlist",
                )
            )
            continue
        selected[normalized] = (name, value)

    _apply_explicit_values(
        selected,
        defined,
        platform=platform,
        source="defined",
        removed=removed,
        violations=violations,
        overridden_names=overridden_names,
        redaction_values=redaction_values,
    )
    _apply_explicit_values(
        selected,
        requested,
        platform=platform,
        source="requested",
        removed=removed,
        violations=violations,
        overridden_names=overridden_names,
        redaction_values=redaction_values,
    )

    variables = tuple(selected[key] for key in sorted(selected))
    _validate_final_environment_size(variables)
    metadata = EnvironmentMetadata(
        inherited_names=_sorted_names(inherited_pairs, platform),
        requested_names=_sorted_names(requested, platform),
        defined_names=_sorted_names(defined, platform),
        included_names=tuple(name for name, _value in variables),
        removed=tuple(
            sorted(
                removed,
                key=lambda item: (
                    normalize_environment_name(item.name, platform),
                    item.reason_code,
                ),
            )
        ),
        overridden_names=tuple(
            sorted(
                set(overridden_names),
                key=lambda name: (normalize_environment_name(name, platform), name),
            )
        ),
    )
    return ConstructedEnvironment(
        variables=variables,
        redaction_values=tuple(redaction_values),
        metadata=metadata,
        violations=tuple(violations),
    )


def redact_environment(
    variables: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    _validate_pairs(variables, "variables")
    return tuple((name, REDACTED_VALUE) for name, _value in variables)


def _allowed_inherited_names(
    platform: ExecutionPlatform,
    policy: EnvironmentPolicy,
) -> frozenset[str]:
    defaults = (
        (*_WINDOWS_INHERITED, *_WINDOWS_HOME_NAMES)
        if platform == "windows" and policy.include_home_paths
        else _WINDOWS_INHERITED
        if platform == "windows"
        else (*_POSIX_INHERITED, *_POSIX_HOME_NAMES)
        if policy.include_home_paths
        else _POSIX_INHERITED
    )
    return frozenset(
        normalize_environment_name(name, platform)
        for name in (*defaults, *policy.additional_inherited_names)
    )


def _apply_explicit_values(
    selected: dict[str, tuple[str, str]],
    values: tuple[tuple[str, str], ...],
    *,
    platform: ExecutionPlatform,
    source: str,
    removed: list[RemovedEnvironmentVariable],
    violations: list[EnvironmentViolation],
    overridden_names: list[str],
    redaction_values: list[str],
) -> None:
    for name, value in values:
        normalized = normalize_environment_name(name, platform)
        match = classify_secret_name(name)
        if match.sensitive:
            removed.append(
                RemovedEnvironmentVariable(
                    name=name,
                    reason_code=f"sensitive-{match.category}",
                )
            )
            violations.append(
                EnvironmentViolation(
                    code=f"sensitive-{source}-environment",
                    name=name,
                    message=(
                        f"{source.capitalize()} environment variable {name!r} "
                        "is classified as sensitive."
                    ),
                )
            )
            _remember_redaction_value(redaction_values, value)
            continue
        previous = selected.get(normalized)
        if previous is not None:
            overridden_names.append(previous[0])
        selected[normalized] = (name, value)


def _remember_redaction_value(values: list[str], value: str) -> None:
    size = len(value.encode("utf-8"))
    if (
        len(value) >= MIN_REDACTION_VALUE_CHARS
        and size <= MAX_REDACTION_VALUE_BYTES
        and value not in values
    ):
        values.append(value)


def _validate_pairs(
    values: object,
    name: str,
    *,
    max_entries: int = MAX_ENVIRONMENT_ENTRIES,
    max_bytes: int = MAX_ENVIRONMENT_BYTES,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple of pairs")
    seen: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(values):
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{name}[{index}] must be a two-item tuple")
        key, value = item
        key = _required_text(key, f"{name}[{index}].key")
        if not isinstance(value, str):
            raise TypeError(f"{name}[{index}].value must be a string")
        if "\x00" in key or "\x00" in value:
            raise ValueError(f"{name}[{index}] must not contain null bytes")
        if "=" in key:
            raise ValueError(f"{name}[{index}].key must not contain '='")
        if key in seen:
            raise ValueError(f"{name} contains duplicate key {key!r}")
        seen.add(key)
        key_bytes = len(key.encode("utf-8"))
        value_bytes = len(value.encode("utf-8"))
        if key_bytes > MAX_ENVIRONMENT_NAME_BYTES:
            raise ValueError(f"{name}[{index}].key is too large")
        if value_bytes > MAX_ENVIRONMENT_VALUE_BYTES:
            raise ValueError(f"{name}[{index}].value is too large")
        total_bytes += key_bytes + 1 + value_bytes
    if len(values) > max_entries:
        raise ValueError(f"{name} contains too many entries")
    if total_bytes > max_bytes:
        raise ValueError(f"{name} exceeds the combined environment byte limit")
    return values


def _validate_final_environment_size(
    values: tuple[tuple[str, str], ...],
) -> None:
    _validate_pairs(values, "constructed environment")


def _validate_names(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    for index, value in enumerate(values):
        _required_text(value, f"{name}[{index}]")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _validate_platform_unique(
    values: tuple[tuple[str, str], ...],
    platform: ExecutionPlatform,
    name: str,
) -> None:
    seen: set[str] = set()
    for key, _value in values:
        normalized = normalize_environment_name(key, platform)
        if normalized in seen:
            raise ValueError(
                f"{name} contains a platform-equivalent duplicate: {key!r}"
            )
        seen.add(normalized)


def _sorted_names(
    values: tuple[tuple[str, str], ...],
    platform: ExecutionPlatform,
) -> tuple[str, ...]:
    return tuple(
        name
        for name, _value in sorted(
            values,
            key=lambda item: (
                normalize_environment_name(item[0], platform),
                item[0],
            ),
        )
    )


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value
