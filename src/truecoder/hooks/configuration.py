from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from platformdirs import user_config_path

from truecoder.hooks.models import (
    DEFAULT_HOOK_TIMEOUT,
    Hook,
    HookConfigError,
    HookSuite,
)

HOOKS_CONFIG_VERSION: Final = 1
MAX_CONFIG_BYTES: Final = 64 * 1024

_HOOK_FIELDS: Final = frozenset(
    {"name", "event", "command", "when", "timeout_seconds", "working_directory"}
)
_ROOT_FIELDS: Final = frozenset({"version", "hooks"})


def default_hooks_config_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "hooks.json"


def load_hooks(path: Path | None = None) -> HookSuite:
    target = path or default_hooks_config_path()
    if not isinstance(target, Path):
        raise HookConfigError("path must be a pathlib.Path")

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HookSuite()
    except (OSError, UnicodeDecodeError) as error:
        return HookSuite(
            unavailable_reason=f"hooks configuration could not be read: {error}"
        )

    try:
        return parse_hooks(raw)
    except HookConfigError as error:
        return HookSuite(unavailable_reason=str(error))


def parse_hooks(raw: str) -> HookSuite:
    if not isinstance(raw, str):
        raise HookConfigError("hooks configuration must be text")
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise HookConfigError("hooks configuration is too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookConfigError(
            f"hooks configuration is not valid JSON: {error}"
        ) from None

    if not isinstance(payload, dict):
        raise HookConfigError("hooks configuration must be a JSON object")

    unknown = set(payload) - _ROOT_FIELDS
    if unknown:
        raise HookConfigError(
            f"unknown hooks configuration fields: {', '.join(sorted(unknown))}"
        )

    version = payload.get("version")
    if version != HOOKS_CONFIG_VERSION:
        raise HookConfigError(
            f"hooks configuration version must be {HOOKS_CONFIG_VERSION}"
        )

    entries = payload.get("hooks", [])
    if not isinstance(entries, list):
        raise HookConfigError("hooks must be a list")

    return HookSuite(hooks=tuple(_hook(entry) for entry in entries))


def _hook(entry: Any) -> Hook:
    if not isinstance(entry, dict):
        raise HookConfigError("each hook must be a JSON object")

    unknown = set(entry) - _HOOK_FIELDS
    if unknown:
        raise HookConfigError(f"unknown hook fields: {', '.join(sorted(unknown))}")

    name = entry.get("name")
    if not isinstance(name, str):
        raise HookConfigError("each hook requires a name")

    command = entry.get("command")
    if not isinstance(command, list):
        raise HookConfigError(f"hook '{name}' requires a command list")

    timeout = entry.get("timeout_seconds", DEFAULT_HOOK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise HookConfigError(f"hook '{name}' has a non-numeric timeout")

    working_directory = entry.get("working_directory", ".")
    if not isinstance(working_directory, str):
        raise HookConfigError(f"hook '{name}' has a non-text working directory")

    return Hook(
        name=name,
        event=entry.get("event"),  # type: ignore[arg-type]
        command=tuple(command),
        condition=entry.get("when", "always"),  # type: ignore[arg-type]
        timeout_seconds=float(timeout),
        working_directory=working_directory,
    )
