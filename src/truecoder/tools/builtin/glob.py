from __future__ import annotations

import asyncio
import fnmatch
import os
from functools import cache
from pathlib import Path, PurePosixPath
from typing import TypedDict

from pydantic import Field

from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.builtin.filesystem import (
    is_sensitive_path,
    resolve_existing_workspace_path,
    validate_workspace_root,
)
from truecoder.tools.context import ToolInvocationContext

MAX_GLOB_MATCHES = 500
MAX_GLOB_SCANNED_ENTRIES = 20_000


class GlobArguments(ToolArguments):
    """Validated arguments accepted by the glob tool."""

    path: str = Field(
        min_length=1,
        description="Directory path relative to the workspace. Use '.' for the root.",
    )
    pattern: str = Field(
        min_length=1,
        description=(
            "Glob pattern relative to path. '*' matches within one directory and "
            "'**' matches across directory levels."
        ),
    )


class GlobOutput(TypedDict):
    path: str
    pattern: str
    matches: list[str]
    has_more: bool


class GlobTool(BaseTool[GlobArguments]):
    """Find paths by a bounded glob search inside a trusted workspace."""

    name = "glob"
    description = (
        "Find files and directories beneath a workspace directory using a glob "
        "pattern such as '*.py' or '**/*.py'."
    )
    arguments_type = GlobArguments
    approval = ToolApproval.REQUIRED

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = validate_workspace_root(workspace_root)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def run(
        self,
        arguments: GlobArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> GlobOutput:
        del invocation
        pattern_parts = self._validate_pattern(arguments.pattern)
        base_directory = resolve_existing_workspace_path(
            self._workspace_root,
            arguments.path,
            expected="directory",
        )
        return await asyncio.to_thread(
            self._search,
            base_directory,
            arguments,
            pattern_parts,
        )

    @staticmethod
    def _validate_pattern(pattern: str) -> tuple[str, ...]:
        posix_pattern = pattern.replace("\\", "/")
        if PurePosixPath(posix_pattern).is_absolute():
            raise ToolExecutionError(
                "Absolute glob patterns are not allowed.",
                code="invalid_pattern",
            )

        parts = tuple(
            part for part in posix_pattern.split("/") if part not in ("", ".")
        )
        if not parts or ".." in parts:
            raise ToolExecutionError(
                "The glob pattern must stay beneath the requested directory.",
                code="invalid_pattern",
            )
        return parts

    @staticmethod
    def _matches(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
        @cache
        def match(pattern_index: int, path_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)

            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                return match(pattern_index + 1, path_index) or (
                    path_index < len(path_parts)
                    and match(pattern_index, path_index + 1)
                )

            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
                and match(pattern_index + 1, path_index + 1)
            )

        return match(0, 0)

    def _search(
        self,
        base_directory: Path,
        arguments: GlobArguments,
        pattern_parts: tuple[str, ...],
    ) -> GlobOutput:
        matches: list[str] = []
        scanned_entries = 0
        scan_limit_reached = False
        pending_directories = [base_directory]

        try:
            while pending_directories:
                directory = pending_directories.pop()
                with os.scandir(directory) as directory_entries:
                    entries = sorted(
                        directory_entries,
                        key=lambda entry: (entry.name.casefold(), entry.name),
                        reverse=True,
                    )

                for entry in entries:
                    scanned_entries += 1
                    if scanned_entries > MAX_GLOB_SCANNED_ENTRIES:
                        scan_limit_reached = True
                        pending_directories.clear()
                        break

                    workspace_path = Path(entry.path).relative_to(self._workspace_root)
                    if is_sensitive_path(workspace_path) or entry.is_symlink():
                        continue

                    relative_to_base = Path(entry.path).relative_to(base_directory)
                    if self._matches(relative_to_base.parts, pattern_parts):
                        matches.append(workspace_path.as_posix())

                    if entry.is_dir(follow_symlinks=False):
                        pending_directories.append(Path(entry.path))
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while searching the workspace.",
                code="permission_denied",
            ) from error
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "A directory disappeared while the search was running.",
                code="file_not_found",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The workspace could not be searched.",
                code="search_failed",
            ) from error

        matches.sort(key=lambda path: (path.casefold(), path))
        return {
            "path": arguments.path,
            "pattern": arguments.pattern,
            "matches": matches[:MAX_GLOB_MATCHES],
            "has_more": scan_limit_reached or len(matches) > MAX_GLOB_MATCHES,
        }
