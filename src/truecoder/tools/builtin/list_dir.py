from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal, TypedDict

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

MAX_DIRECTORY_ENTRIES = 500
MAX_SCANNED_DIRECTORY_ENTRIES = 5_000


class ListDirArguments(ToolArguments):
    """Validated arguments accepted by the directory-listing tool."""

    path: str = Field(
        min_length=1,
        description="Directory path relative to the workspace. Use '.' for the root.",
    )


class ListDirEntry(TypedDict):
    path: str
    name: str
    type: Literal["directory", "file", "symlink", "other"]


class ListDirOutput(TypedDict):
    path: str
    entries: list[ListDirEntry]
    has_more: bool


class ListDirTool(BaseTool[ListDirArguments]):
    """List one bounded directory level inside a trusted workspace."""

    name = "list_dir"
    description = (
        "List the immediate entries in a workspace directory. "
        "The result is bounded and does not recurse."
    )
    arguments_type = ListDirArguments
    approval = ToolApproval.REQUIRED

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = validate_workspace_root(workspace_root)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def run(self, arguments: ListDirArguments) -> ListDirOutput:
        directory = resolve_existing_workspace_path(
            self._workspace_root,
            arguments.path,
            expected="directory",
        )
        return await asyncio.to_thread(
            self._list_directory,
            directory,
            arguments.path,
        )

    def _list_directory(
        self,
        directory: Path,
        requested_path: str,
    ) -> ListDirOutput:
        entries: list[ListDirEntry] = []
        visible_entry_count = 0
        scan_limit_reached = False

        try:
            with os.scandir(directory) as directory_entries:
                for scanned_count, entry in enumerate(directory_entries, start=1):
                    if scanned_count > MAX_SCANNED_DIRECTORY_ENTRIES:
                        scan_limit_reached = True
                        break

                    relative_path = Path(entry.path).relative_to(self._workspace_root)
                    if is_sensitive_path(relative_path):
                        continue

                    visible_entry_count += 1
                    if len(entries) >= MAX_DIRECTORY_ENTRIES:
                        continue

                    if entry.is_symlink():
                        entry_type: Literal[
                            "directory", "file", "symlink", "other"
                        ] = "symlink"
                    elif entry.is_dir(follow_symlinks=False):
                        entry_type = "directory"
                    elif entry.is_file(follow_symlinks=False):
                        entry_type = "file"
                    else:
                        entry_type = "other"

                    entries.append(
                        {
                            "path": relative_path.as_posix(),
                            "name": entry.name,
                            "type": entry_type,
                        }
                    )
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while listing the directory.",
                code="permission_denied",
            ) from error
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The requested directory no longer exists.",
                code="file_not_found",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The directory could not be listed.",
                code="list_failed",
            ) from error

        entries.sort(
            key=lambda item: (
                item["type"] != "directory",
                item["name"].casefold(),
                item["name"],
            )
        )
        return {
            "path": requested_path,
            "entries": entries,
            "has_more": scan_limit_reached
            or visible_entry_count > MAX_DIRECTORY_ENTRIES,
        }
