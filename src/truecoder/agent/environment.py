from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

VIRTUAL_ENVIRONMENT_DIRECTORIES: Final = (".venv", "venv", ".virtualenv", "env")

_POSIX_INTERPRETERS: Final = ("bin/python3", "bin/python")
_WINDOWS_INTERPRETERS: Final = ("Scripts/python.exe",)


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    working_directory: str
    operating_system: str
    interpreter: str
    interpreter_version: str
    workspace_interpreter: str | None = None

    def __post_init__(self) -> None:
        for name in ("working_directory", "operating_system", "interpreter"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if self.workspace_interpreter is not None and not isinstance(
            self.workspace_interpreter,
            str,
        ):
            raise TypeError("workspace_interpreter must be text or None")


def find_workspace_interpreter(project_root: Path) -> str | None:
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a pathlib.Path")

    relative_names = _WINDOWS_INTERPRETERS if _is_windows() else _POSIX_INTERPRETERS
    for directory in VIRTUAL_ENVIRONMENT_DIRECTORIES:
        for relative in relative_names:
            candidate = project_root / directory / relative
            try:
                if candidate.is_file():
                    return str(Path(directory) / relative)
            except OSError:
                continue
    return None


def collect_environment(project_root: Path) -> EnvironmentFacts:
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a pathlib.Path")

    release = platform.release().strip()
    system = platform.system().strip() or "unknown"
    return EnvironmentFacts(
        working_directory=str(project_root),
        operating_system=f"{system} {release}".strip(),
        interpreter=sys.executable or "unknown",
        interpreter_version=platform.python_version(),
        workspace_interpreter=find_workspace_interpreter(project_root),
    )


def describe_environment(facts: EnvironmentFacts) -> str:
    if not isinstance(facts, EnvironmentFacts):
        raise TypeError("facts must be EnvironmentFacts")

    lines = [
        f"Working directory: {facts.working_directory}",
        f"Operating system: {facts.operating_system}",
        (
            f"TrueCoder is running on {facts.interpreter} "
            f"(Python {facts.interpreter_version})"
        ),
    ]
    if facts.workspace_interpreter is not None:
        lines.append(f"Workspace virtual environment: {facts.workspace_interpreter}")
    else:
        lines.append("Workspace virtual environment: none found in the project root")

    body = "\n".join(lines)
    return f"<environment>\n{body}\n</environment>"


def _is_windows() -> bool:
    return platform.system().strip().lower() == "windows"
