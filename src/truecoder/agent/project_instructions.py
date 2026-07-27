from __future__ import annotations

from pathlib import Path

PROJECT_INSTRUCTIONS_MAX_BYTES = 32 * 1024


class ProjectInstructionsError(RuntimeError):
    """Project instructions could not be loaded safely."""


def _resolve_directory(value: object, *, parameter: str) -> Path:
    """Validate and resolve a directory argument."""
    if not isinstance(value, Path):
        raise TypeError(f"{parameter} must be a pathlib.Path")

    try:
        resolved = value.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ProjectInstructionsError(
            f"{parameter} does not resolve to an existing path: {value}"
        ) from exc

    if not resolved.is_dir():
        raise ProjectInstructionsError(
            f"{parameter} must resolve to a directory: {value}"
        )

    return resolved


def _validate_max_bytes(max_bytes: object) -> int:
    """Validate the project-instructions size limit."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be a positive integer")

    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    return max_bytes


def _validate_project_paths(
    *,
    project_root: object,
    launch_directory: object,
) -> tuple[Path, Path]:
    """Resolve both paths and verify their containment relationship."""
    resolved_root = _resolve_directory(
        project_root,
        parameter="project_root",
    )
    resolved_launch = _resolve_directory(
        launch_directory,
        parameter="launch_directory",
    )

    try:
        resolved_launch.relative_to(resolved_root)
    except ValueError as exc:
        raise ProjectInstructionsError(
            "launch_directory must be inside project_root"
        ) from exc

    return resolved_root, resolved_launch


def find_project_root(launch_directory: Path) -> Path:
    launch = _resolve_directory(launch_directory, parameter="launch_directory")
    if not launch.is_dir():
        raise RuntimeError("The path is a file, a special file, or does not exist.")

    project_root: Path = launch
    paths_to_check = [launch] + list(launch.parents)

    for path in paths_to_check:
        git_entry = path / ".git"
        if git_entry.exists():
            project_root = path
            break

    return project_root


_INSTRUCTION_FILENAMES = (
    "AGENTS.override.md",
    "AGENTS.md",
)


def _discover_instruction_entries(
    *,
    project_root: Path,
    launch_directory: Path,
) -> tuple[tuple[Path, str], ...]:
    launch = _resolve_directory(
        launch_directory,
        parameter="launch_directory",
    )
    project = _resolve_directory(
        project_root,
        parameter="project_root",
    )

    try:
        launch.relative_to(project)
    except ValueError as exc:
        raise ProjectInstructionsError(
            f"launch_directory is outside project_root: {launch}"
        ) from exc

    directory_chain: list[Path] = []
    current = launch

    while True:
        directory_chain.append(current)

        if current == project:
            break

        current = current.parent

    directory_chain.reverse()

    selected: list[tuple[Path, str]] = []

    for directory in directory_chain:
        for filename in _INSTRUCTION_FILENAMES:
            candidate = directory / filename

            if not candidate.exists():
                if candidate.is_symlink():
                    raise ProjectInstructionsError(
                        f"Project instructions could not be read: {candidate}"
                    )
                continue

            if not candidate.is_file():
                raise ProjectInstructionsError(
                    f"Project instructions are not a regular file: {candidate}"
                )

            try:
                content = candidate.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise ProjectInstructionsError(
                    f"Project instructions could not be read: {candidate}"
                ) from exc

            if content:
                selected.append((candidate, content))
                break

    return tuple(selected)


def discover_instruction_files(
    *,
    project_root: Path,
    launch_directory: Path,
) -> tuple[Path, ...]:
    entries = _discover_instruction_entries(
        project_root=project_root,
        launch_directory=launch_directory,
    )

    return tuple(path for path, _content in entries)


def load_project_instructions(
    *,
    project_root: Path,
    launch_directory: Path,
    max_bytes: int = PROJECT_INSTRUCTIONS_MAX_BYTES,
) -> str:
    """Load project instructions ordered from general to most specific."""
    validated_max_bytes = _validate_max_bytes(max_bytes)

    resolved_root, resolved_launch = _validate_project_paths(
        project_root=project_root,
        launch_directory=launch_directory,
    )

    entries = _discover_instruction_entries(
        project_root=resolved_root,
        launch_directory=resolved_launch,
    )

    sections: list[str] = []
    used_bytes = 0

    for _path, content in entries:
        separator_bytes = 2 if sections else 0
        remaining_bytes = validated_max_bytes - used_bytes - separator_bytes

        if remaining_bytes <= 0:
            break

        encoded_content = content.encode("utf-8")
        if len(encoded_content) <= remaining_bytes:
            sections.append(content)
            used_bytes += separator_bytes + len(encoded_content)
            continue

        truncated_content = encoded_content[:remaining_bytes].decode(
            "utf-8",
            errors="ignore",
        )
        if truncated_content:
            sections.append(truncated_content)
        break

    return "\n\n".join(sections)
