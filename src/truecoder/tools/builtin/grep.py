from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path
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

MAX_GREP_MATCHES = 200
MAX_GREP_PATTERN_CHARACTERS = 1_024
MAX_GREP_SCANNED_ENTRIES = 20_000
MAX_GREP_FILE_BYTES = 1024 * 1024
MAX_GREP_LINE_CHARACTERS = 500
_TEXT_CONTROL_BYTES = frozenset({9, 10, 13})


class GrepArguments(ToolArguments):
    """Validated arguments accepted by the grep tool."""

    path: str = Field(
        min_length=1,
        description=(
            "File or directory path relative to the workspace. "
            "Directories are searched recursively."
        ),
    )
    pattern: str = Field(
        min_length=1,
        max_length=MAX_GREP_PATTERN_CHARACTERS,
        description="Python regular expression to find in UTF-8 text lines.",
    )


class GrepMatch(TypedDict):
    path: str
    line_number: int
    line: str


class GrepOutput(TypedDict):
    path: str
    pattern: str
    matches: list[GrepMatch]
    has_more: bool


class GrepTool(BaseTool[GrepArguments]):
    """Search bounded UTF-8 workspace text using a regular expression."""

    name = "grep"
    description = (
        "Search a workspace file or directory for a Python regular expression. "
        "Returns matching UTF-8 text lines with paths and line numbers."
    )
    arguments_type = GrepArguments
    approval = ToolApproval.REQUIRED

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = validate_workspace_root(workspace_root)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def run(self, arguments: GrepArguments) -> GrepOutput:
        try:
            expression = re.compile(arguments.pattern)
        except re.error as error:
            raise ToolExecutionError(
                f"The regular expression is invalid: {error}.",
                code="invalid_pattern",
            ) from error

        requested_path = resolve_existing_workspace_path(
            self._workspace_root,
            arguments.path,
            expected="file_or_directory",
        )
        return await asyncio.to_thread(
            self._search,
            requested_path,
            arguments,
            expression,
        )

    def _search(
        self,
        requested_path: Path,
        arguments: GrepArguments,
        expression: re.Pattern[str],
    ) -> GrepOutput:
        files, scan_limit_reached = self._collect_files(requested_path)
        matches: list[GrepMatch] = []
        result_limit_reached = False

        for file in files:
            for match in self._search_file(file, expression):
                if len(matches) == MAX_GREP_MATCHES:
                    result_limit_reached = True
                    break
                matches.append(match)
            if result_limit_reached:
                break

        return {
            "path": arguments.path,
            "pattern": arguments.pattern,
            "matches": matches,
            "has_more": scan_limit_reached or result_limit_reached,
        }

    def _collect_files(self, requested_path: Path) -> tuple[list[Path], bool]:
        if requested_path.is_file():
            return [requested_path], False

        files: list[Path] = []
        scanned_entries = 0
        scan_limit_reached = False
        pending_directories = [requested_path]

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
                    if scanned_entries > MAX_GREP_SCANNED_ENTRIES:
                        scan_limit_reached = True
                        pending_directories.clear()
                        break

                    workspace_path = Path(entry.path).relative_to(self._workspace_root)
                    if is_sensitive_path(workspace_path) or entry.is_symlink():
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        pending_directories.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        files.append(Path(entry.path))
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

        files.sort(
            key=lambda file: (
                file.relative_to(self._workspace_root).as_posix().casefold(),
                file.relative_to(self._workspace_root).as_posix(),
            )
        )
        return files, scan_limit_reached

    def _search_file(
        self,
        file: Path,
        expression: re.Pattern[str],
    ) -> Iterator[GrepMatch]:
        try:
            with file.open("rb") as searched_file:
                raw_content = searched_file.read(MAX_GREP_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while reading a searched file.",
                code="permission_denied",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "A searched file could not be read.",
                code="search_failed",
            ) from error

        if len(raw_content) > MAX_GREP_FILE_BYTES or any(
            (byte < 32 and byte not in _TEXT_CONTROL_BYTES) or byte == 127
            for byte in raw_content
        ):
            return

        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            return

        workspace_path = file.relative_to(self._workspace_root).as_posix()
        for line_number, line in enumerate(content.splitlines(), start=1):
            if expression.search(line) is None:
                continue

            display_line = line
            if len(display_line) > MAX_GREP_LINE_CHARACTERS:
                display_line = f"{display_line[:MAX_GREP_LINE_CHARACTERS]}…"
            yield {
                "path": workspace_path,
                "line_number": line_number,
                "line": display_line,
            }
