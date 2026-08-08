from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def is_workspace_relative(requested: object) -> bool:
    if not isinstance(requested, str) or not requested.strip():
        return False

    return not (PurePosixPath(requested).anchor or PureWindowsPath(requested).anchor)


def resolve_inside_workspace(
    project_root: Path,
    requested: str,
    *,
    subject: str = "working directory",
) -> Path:
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a pathlib.Path")
    if not is_workspace_relative(requested):
        raise ValueError(f"a {subject} must be workspace-relative")

    root = project_root.resolve()
    resolved = (root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"a {subject} must stay inside the workspace")
    return resolved
