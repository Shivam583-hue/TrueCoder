from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import TypedDict

from pydantic import Field

from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)

MAX_LINE_COUNT = 500

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
_SAFE_ENV_TEMPLATE_NAMES = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)
_TEXT_CONTROL_BYTES = frozenset({9, 10, 13})


class ReadFileArguments(ToolArguments):
    """Validated arguments accepted by the read-file tool."""

    path: str = Field(
        min_length=1,
        description="Path to the file, relative to the workspace.",
    )
    start_line: int = Field(
        ge=1,
        description="One-based line number at which to start reading.",
    )
    line_count: int = Field(
        ge=1,
        le=MAX_LINE_COUNT,
        description=f"Maximum number of lines to return, up to {MAX_LINE_COUNT}.",
    )


class ReadFileOutput(TypedDict):
    """Structured output returned by the read file tool."""

    path: str
    content: str
    start_line: int
    end_line: int
    has_more: bool


class ReadFileTool(BaseTool[ReadFileArguments]):
    """Read bounded UTF-8 text ranges from a trusted workspace."""

    name = "read_file"
    description = "Read a range of lines from a file in the workspace."
    arguments_type = ReadFileArguments
    approval = ToolApproval.REQUIRED

    def __init__(self, workspace_root: Path) -> None:
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

        self._workspace_root = resolved_root

    @property
    def workspace_root(self) -> Path:
        """Return the trusted, resolved workspace root."""

        return self._workspace_root

    async def run(self, arguments: ReadFileArguments) -> ReadFileOutput:
        resolved_path = self._resolve_requested_path(arguments.path)
        return await asyncio.to_thread(
            self._read_lines,
            resolved_path,
            arguments,
        )

    def _resolve_requested_path(self, requested_path: str) -> Path:
        relative_path = Path(requested_path)
        if relative_path.is_absolute():
            raise ToolExecutionError(
                "Absolute paths cannot be read.",
                code="outside_workspace",
            )

        try:
            resolved_path = (self._workspace_root / relative_path).resolve(
                strict=False
            )
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

        if not resolved_path.is_relative_to(self._workspace_root):
            raise ToolExecutionError(
                "The requested path is outside the workspace.",
                code="outside_workspace",
            )

        workspace_path = resolved_path.relative_to(self._workspace_root)
        if self._is_sensitive_path(workspace_path):
            raise ToolExecutionError(
                "The requested path is considered sensitive.",
                code="sensitive_path",
            )

        try:
            path_stat = resolved_path.stat()
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The requested file does not exist.",
                code="file_not_found",
            ) from error
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while accessing the requested file.",
                code="permission_denied",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The requested path could not be inspected.",
                code="not_a_file",
            ) from error

        if not stat.S_ISREG(path_stat.st_mode):
            raise ToolExecutionError(
                "The requested path is not a regular file.",
                code="not_a_file",
            )

        return resolved_path

    @staticmethod
    def _is_sensitive_path(workspace_path: Path) -> bool:
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

    @classmethod
    def _decode_line(cls, raw_line: bytes) -> str:
        if cls._contains_binary_control_bytes(raw_line):
            raise ToolExecutionError(
                "The requested file is not supported UTF-8 text.",
                code="unsupported_encoding",
            )

        try:
            return raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "The requested file is not supported UTF-8 text.",
                code="unsupported_encoding",
            ) from error

    @staticmethod
    def _contains_binary_control_bytes(raw_line: bytes) -> bool:
        return any(
            (byte < 32 and byte not in _TEXT_CONTROL_BYTES) or byte == 127
            for byte in raw_line
        )

    def _read_lines(
        self,
        resolved_path: Path,
        arguments: ReadFileArguments,
    ) -> ReadFileOutput:
        returned_lines: list[str] = []
        has_more = False

        try:
            with resolved_path.open("rb") as file:
                for line_number, raw_line in enumerate(file, start=1):
                    decoded_line = self._decode_line(raw_line)

                    if line_number < arguments.start_line:
                        continue

                    if len(returned_lines) == arguments.line_count:
                        has_more = True
                        break

                    returned_lines.append(decoded_line)
        except ToolExecutionError:
            raise
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The requested file no longer exists.",
                code="file_not_found",
            ) from error
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while reading the requested file.",
                code="permission_denied",
            ) from error
        except IsADirectoryError as error:
            raise ToolExecutionError(
                "The requested path is not a regular file.",
                code="not_a_file",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The requested file could not be read.",
                code="read_failed",
            ) from error

        actual_end_line = arguments.start_line + len(returned_lines) - 1
        return {
            "path": arguments.path,
            "content": "".join(returned_lines),
            "start_line": arguments.start_line,
            "end_line": actual_end_line,
            "has_more": has_more,
        }
