from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.errors import AuditUnavailableError

SETTINGS_VERSION: Final = 1
MAX_SETTINGS_BYTES: Final = 64 * 1024
MAX_VALUE_CHARACTERS: Final = 200

_ROOT_FIELDS: Final = frozenset({"version", "model", "provider", "reasoning_effort"})


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredSelection:
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.model is None
            and self.provider is None
            and self.reasoning_effort is None
        )


def default_settings_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "settings.json"


def _text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{field} must be text")
    collapsed = value.strip()
    if not collapsed:
        return None
    if len(collapsed) > MAX_VALUE_CHARACTERS:
        raise SettingsError(f"{field} is longer than allowed")
    return collapsed


def _reasoning_effort(value: object) -> str | None:
    effort = _text(value, "reasoning_effort")
    if effort is None:
        return None
    from truecoder.providers.models import CredentialError, validate_reasoning_effort

    try:
        return validate_reasoning_effort(effort)
    except CredentialError as error:
        raise SettingsError(str(error)) from None


def parse_selection(raw: str) -> StoredSelection:
    if not isinstance(raw, str):
        raise SettingsError("settings must be text")
    if len(raw.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise SettingsError("settings are too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsError(f"settings are not valid JSON: {error}") from None

    if not isinstance(payload, dict):
        raise SettingsError("settings must be a JSON object")

    unknown = set(payload) - _ROOT_FIELDS
    if unknown:
        raise SettingsError(f"unknown settings field(s): {sorted(unknown)}")

    if payload.get("version") != SETTINGS_VERSION:
        raise SettingsError(f"settings version must be {SETTINGS_VERSION}")

    return StoredSelection(
        model=_text(payload.get("model"), "model"),
        provider=_text(payload.get("provider"), "provider"),
        reasoning_effort=_reasoning_effort(payload.get("reasoning_effort")),
    )


def encode_selection(selection: StoredSelection) -> str:
    payload: dict[str, object] = {"version": SETTINGS_VERSION}
    if selection.model is not None:
        payload["model"] = selection.model
    if selection.provider is not None:
        payload["provider"] = selection.provider
    if selection.reasoning_effort is not None:
        payload["reasoning_effort"] = selection.reasoning_effort
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_selection(path: Path | None = None) -> StoredSelection:
    target = path or default_settings_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StoredSelection()
    except (OSError, UnicodeDecodeError):
        return StoredSelection()

    try:
        return parse_selection(raw)
    except SettingsError:
        return StoredSelection()


def save_selection(
    selection: StoredSelection,
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    target = path or default_settings_path()
    guard = permissions or AuditPermissions()

    try:
        guard.prepare(target)
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".settings-",
            suffix=".json",
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(encode_selection(selection))
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, AuditUnavailableError):
        return False

    guard.secure_sidecars(target)
    return True
