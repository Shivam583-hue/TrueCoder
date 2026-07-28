from __future__ import annotations

import stat
from pathlib import Path
from typing import Literal

from truecoder.tools.base import ToolExecutionError

_SENSITIVE_DIRECTORY_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".credentials",
        ".netrc",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_SENSITIVE_FILE_SUFFIXES = frozenset({".jks", ".key", ".p12", ".pem", ".pfx"})
_SAFE_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_sensitive_path(workspace_path: Path) -> bool:
    normalized_parts = tuple(part.casefold() for part in workspace_path.parts)
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in normalized_parts):
        return True

    file_name = workspace_path.name.casefold()
    if file_name in _SAFE_ENV_TEMPLATE_NAMES:
        return False

    if file_name == ".env" or file_name.startswith(".env."):
        return True

    if file_name in _SENSITIVE_FILE_NAMES:
        return True

    return workspace_path.suffix.casefold() in _SENSITIVE_FILE_SUFFIXES


def validate_workspace_root(workspace_root: Path) -> Path:
    """Return a trusted canonical workspace root."""
    if not isinstance(workspace_root, Path):
        raise TypeError("workspace_root must be a pathlib.Path.")
    if not workspace_root.is_absolute():
        raise ValueError("workspace_root must be an absolute path.")

    try:
        resolved_root = workspace_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("workspace_root must exist and be accessible.") from error
    if not resolved_root.is_dir():
        raise ValueError("workspace_root must be a directory.")
    return resolved_root


def resolve_existing_workspace_path(
    workspace_root: Path,
    requested_path: str,
    *,
    expected: Literal["file", "directory", "file_or_directory"],
    allow_symlinks: bool = False,
) -> Path:
    """Resolve an existing non-sensitive path beneath a trusted workspace."""
    relative_path = Path(requested_path)
    if relative_path.is_absolute():
        raise ToolExecutionError(
            "Absolute paths are not allowed.",
            code="outside_workspace",
        )
    if is_sensitive_path(relative_path):
        raise ToolExecutionError(
            "The requested path is considered sensitive.",
            code="sensitive_path",
        )

    candidate = workspace_root / relative_path
    if not allow_symlinks:
        current = candidate
        while current != workspace_root and current != current.parent:
            try:
                if current.is_symlink():
                    raise ToolExecutionError(
                        "Symbolic links are not allowed in this path.",
                        code="symlink_not_allowed",
                    )
            except PermissionError as error:
                raise ToolExecutionError(
                    "Permission was denied while inspecting the requested path.",
                    code="permission_denied",
                ) from error
            current = current.parent

    try:
        unresolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ToolExecutionError(
            "The requested path could not be resolved safely.",
            code="outside_workspace",
        ) from error
    if not unresolved_candidate.is_relative_to(workspace_root):
        raise ToolExecutionError(
            "The requested path is outside the workspace.",
            code="outside_workspace",
        )

    try:
        resolved_path = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise ToolExecutionError(
            "The requested path does not exist.",
            code="file_not_found",
        ) from error
    except PermissionError as error:
        raise ToolExecutionError(
            "Permission was denied while resolving the requested path.",
            code="permission_denied",
        ) from error
    except (OSError, RuntimeError) as error:
        raise ToolExecutionError(
            "The requested path could not be resolved safely.",
            code="outside_workspace",
        ) from error

    if not resolved_path.is_relative_to(workspace_root):
        raise ToolExecutionError(
            "The requested path is outside the workspace.",
            code="outside_workspace",
        )

    workspace_path = resolved_path.relative_to(workspace_root)
    if is_sensitive_path(workspace_path):
        raise ToolExecutionError(
            "The requested path is considered sensitive.",
            code="sensitive_path",
        )

    try:
        path_stat = resolved_path.stat()
    except PermissionError as error:
        raise ToolExecutionError(
            "Permission was denied while inspecting the requested path.",
            code="permission_denied",
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "The requested path could not be inspected.",
            code=f"not_a_{expected}",
        ) from error

    if expected == "file":
        expected_mode = stat.S_ISREG(path_stat.st_mode)
        error_code = "not_a_file"
        error_message = "The requested path is not a file."
    elif expected == "directory":
        expected_mode = stat.S_ISDIR(path_stat.st_mode)
        error_code = "not_a_directory"
        error_message = "The requested path is not a directory."
    else:
        expected_mode = stat.S_ISREG(path_stat.st_mode) or stat.S_ISDIR(
            path_stat.st_mode
        )
        error_code = "unsupported_path"
        error_message = "The requested path is not a regular file or directory."

    if not expected_mode:
        raise ToolExecutionError(
            error_message,
            code=error_code,
        )
    return resolved_path
