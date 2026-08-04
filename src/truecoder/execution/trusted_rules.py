from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.execution.models import ExecutionLimits, RiskLevel

TRUSTED_RULES_SCHEMA_VERSION: Final = 1
MAX_RULES: Final = 500
MAX_RULE_ID_CHARS: Final = 64
MAX_EXECUTABLE_CHARS: Final = 128

_ALLOWED_RISK = {level.value: level for level in RiskLevel}


class TrustedRulesError(ValueError):
    pass


def default_trusted_rules_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "trusted-commands.json"


@dataclass(frozen=True, slots=True)
class TrustedCommandRule:
    rule_id: str
    executable: str
    max_risk: RiskLevel
    require_approval: bool = True

    def __post_init__(self) -> None:
        _require_identifier(self.rule_id, "rule_id", MAX_RULE_ID_CHARS)
        _require_identifier(self.executable, "executable", MAX_EXECUTABLE_CHARS)
        if "/" in self.executable or "\\" in self.executable:
            raise TrustedRulesError(
                "executable must be a bare program name, not a path"
            )
        if not isinstance(self.max_risk, RiskLevel):
            raise TrustedRulesError("max_risk must be a RiskLevel")
        if not isinstance(self.require_approval, bool):
            raise TrustedRulesError("require_approval must be a boolean")


@dataclass(frozen=True, slots=True)
class TrustedRuleSet:
    version: int = TRUSTED_RULES_SCHEMA_VERSION
    rules: tuple[TrustedCommandRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TrustedRulesError("version must be an integer")
        if self.version != TRUSTED_RULES_SCHEMA_VERSION:
            raise TrustedRulesError(
                f"unsupported trusted rules version: {self.version}"
            )
        if not isinstance(self.rules, tuple):
            raise TrustedRulesError("rules must be a tuple")
        if len(self.rules) > MAX_RULES:
            raise TrustedRulesError(f"at most {MAX_RULES} rules are allowed")
        seen_ids = set()
        seen_executables = set()
        for rule in self.rules:
            if not isinstance(rule, TrustedCommandRule):
                raise TrustedRulesError("rules must contain TrustedCommandRule values")
            if rule.rule_id in seen_ids:
                raise TrustedRulesError(f"duplicate rule id: {rule.rule_id}")
            if rule.executable in seen_executables:
                raise TrustedRulesError(
                    f"duplicate executable rule: {rule.executable}"
                )
            seen_ids.add(rule.rule_id)
            seen_executables.add(rule.executable)

    def rule_for(self, executable: str) -> TrustedCommandRule | None:
        for rule in self.rules:
            if rule.executable == executable:
                return rule
        return None

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "rules": [
                    {
                        "rule_id": rule.rule_id,
                        "executable": rule.executable,
                        "max_risk": rule.max_risk.value,
                        "require_approval": rule.require_approval,
                    }
                    for rule in sorted(self.rules, key=lambda item: item.rule_id)
                ],
            },
            indent=2,
            sort_keys=True,
        )


def parse_trusted_rules(raw: str) -> TrustedRuleSet:
    if not isinstance(raw, str):
        raise TrustedRulesError("trusted rules must be provided as text")
    if len(raw) > 1024 * 1024:
        raise TrustedRulesError("trusted rules document is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TrustedRulesError(f"trusted rules are not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise TrustedRulesError("trusted rules must be a JSON object")

    version = payload.get("version")
    entries = payload.get("rules", [])
    if not isinstance(entries, list):
        raise TrustedRulesError("rules must be a JSON array")

    rules: list[TrustedCommandRule] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TrustedRulesError(f"rules[{index}] must be an object")
        unknown = set(entry) - {
            "rule_id",
            "executable",
            "max_risk",
            "require_approval",
        }
        if unknown:
            raise TrustedRulesError(
                f"rules[{index}] has unknown fields: {sorted(unknown)}"
            )
        raw_risk = entry.get("max_risk")
        if raw_risk not in _ALLOWED_RISK:
            raise TrustedRulesError(
                f"rules[{index}] has an unknown max_risk: {raw_risk!r}"
            )
        require_approval = entry.get("require_approval", True)
        if not isinstance(require_approval, bool):
            raise TrustedRulesError(
                f"rules[{index}] require_approval must be a boolean"
            )
        rules.append(
            TrustedCommandRule(
                rule_id=str(entry.get("rule_id", "")),
                executable=str(entry.get("executable", "")),
                max_risk=_ALLOWED_RISK[raw_risk],
                require_approval=require_approval,
            )
        )

    return TrustedRuleSet(
        version=version if version is not None else TRUSTED_RULES_SCHEMA_VERSION,
        rules=tuple(rules),
    )


def load_trusted_rules(path: Path | None = None) -> TrustedRuleSet:
    target = path or default_trusted_rules_path()
    if not isinstance(target, Path):
        raise TrustedRulesError("path must be a pathlib.Path")
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return TrustedRuleSet()
    except (OSError, UnicodeDecodeError) as error:
        raise TrustedRulesError(f"trusted rules could not be read: {error}") from None
    return parse_trusted_rules(raw)


def save_trusted_rules(rules: TrustedRuleSet, path: Path | None = None) -> Path:
    if not isinstance(rules, TrustedRuleSet):
        raise TrustedRulesError("rules must be a TrustedRuleSet")
    target = path or default_trusted_rules_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(rules.to_json() + "\n", encoding="utf-8")
    try:
        target.parent.chmod(0o700)
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)
    return target


def apply_trusted_rules(
    rules: TrustedRuleSet,
    *,
    executable: str,
    risk: RiskLevel,
    requires_approval: bool,
    ceiling: ExecutionLimits,
    requested: ExecutionLimits,
) -> tuple[RiskLevel, bool, tuple[str, ...]]:
    del ceiling, requested
    rule = rules.rule_for(executable)
    if rule is None:
        return risk, requires_approval, ()

    if _risk_rank(risk) > _risk_rank(rule.max_risk):
        return (
            risk,
            requires_approval,
            (f"policy.020.trusted.rejected.{rule.rule_id}",),
        )

    effective_approval = requires_approval and rule.require_approval
    if requires_approval and not effective_approval and risk is not RiskLevel.LOW:
        effective_approval = True
        return risk, effective_approval, (
            f"policy.020.trusted.approval_retained.{rule.rule_id}",
        )

    return risk, effective_approval, (f"policy.020.trusted.applied.{rule.rule_id}",)


_RISK_ORDER: Final = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}


def _risk_rank(level: RiskLevel) -> int:
    return _RISK_ORDER.get(level, len(_RISK_ORDER))


def _require_identifier(value: object, name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TrustedRulesError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise TrustedRulesError(f"{name} must not be empty")
    if len(stripped) > maximum:
        raise TrustedRulesError(f"{name} must be at most {maximum} characters")
    if any(character.isspace() or character == "\x00" for character in stripped):
        raise TrustedRulesError(f"{name} must not contain whitespace or null bytes")
