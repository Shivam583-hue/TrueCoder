from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from dataclasses import dataclass
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
    resolve_existing_workspace_path,
    validate_workspace_root,
)

MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_EDIT_TEXT_BYTES = 32 * 1024
_TEXT_CONTROL_CHARACTERS = frozenset({"\t", "\n", "\r"})


class EditFileArguments(ToolArguments):
    """Validated arguments accepted by the exact file-editing tool."""

    path: str = Field(
        min_length=1,
        description="Existing UTF-8 text file path relative to the workspace.",
    )
    old_text: str = Field(
        min_length=1,
        description="Exact text that currently exists in the file.",
    )
    new_text: str = Field(
        description="Text that replaces old_text. Use an empty string to delete it.",
    )
    replace_all: bool = Field(
        description=(
            "Replace every exact occurrence when true. When false, old_text must "
            "occur exactly once."
        ),
    )


class EditFileOutput(TypedDict):
    path: str
    replacements: int
    bytes_written: int


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def from_stat(cls, file_stat: os.stat_result) -> _FileFingerprint:
        return cls(
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            size=file_stat.st_size,
            modified_ns=file_stat.st_mtime_ns,
        )


class EditFileTool(BaseTool[EditFileArguments]):
    """Atomically replace exact text in an existing workspace file."""

    name = "edit_file"
    description = (
        "Replace exact text in an existing UTF-8 workspace file. Set replace_all "
        "to false for one unique occurrence or true for every occurrence."
    )
    arguments_type = EditFileArguments
    approval = ToolApproval.REQUIRED

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = validate_workspace_root(workspace_root)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def run(self, arguments: EditFileArguments) -> EditFileOutput:
        old_text = self._encode_edit_text(arguments.old_text, field_name="old_text")
        new_text = self._encode_edit_text(arguments.new_text, field_name="new_text")
        if len(old_text) > MAX_EDIT_TEXT_BYTES or len(new_text) > MAX_EDIT_TEXT_BYTES:
            raise ToolExecutionError(
                f"Edit text exceeds the maximum size of {MAX_EDIT_TEXT_BYTES} bytes.",
                code="edit_too_large",
            )

        return await asyncio.to_thread(self._edit_atomic, arguments)

    @staticmethod
    def _encode_edit_text(text: str, *, field_name: str) -> bytes:
        for character in text:
            code_point = ord(character)
            if (
                code_point < 0x20 and character not in _TEXT_CONTROL_CHARACTERS
            ) or code_point == 0x7F:
                raise ToolExecutionError(
                    f"{field_name} contains an unsupported control character.",
                    code="unsupported_content",
                )

        try:
            return text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolExecutionError(
                f"{field_name} could not be encoded as UTF-8.",
                code="unsupported_content",
            ) from error

    def _edit_atomic(self, arguments: EditFileArguments) -> EditFileOutput:
        destination = resolve_existing_workspace_path(
            self._workspace_root,
            arguments.path,
            expected="file",
        )
        original_content, fingerprint, destination_mode = self._read_original(
            destination
        )

        occurrence_count = original_content.count(arguments.old_text)
        if occurrence_count == 0:
            raise ToolExecutionError(
                "old_text was not found in the file.",
                code="text_not_found",
            )
        if not arguments.replace_all and occurrence_count != 1:
            raise ToolExecutionError(
                (
                    f"old_text occurs {occurrence_count} times. Provide more "
                    "surrounding text or set replace_all to true."
                ),
                code="ambiguous_match",
            )

        replacements = occurrence_count if arguments.replace_all else 1
        edited_content = original_content.replace(
            arguments.old_text,
            arguments.new_text,
            -1 if arguments.replace_all else 1,
        )
        if edited_content == original_content:
            raise ToolExecutionError(
                "The requested replacement would not change the file.",
                code="no_change",
            )

        encoded_content = edited_content.encode("utf-8")
        if len(encoded_content) > MAX_EDIT_FILE_BYTES:
            raise ToolExecutionError(
                f"The edited file would exceed {MAX_EDIT_FILE_BYTES} bytes.",
                code="file_too_large",
            )

        self._replace_atomically(
            destination,
            arguments.path,
            encoded_content,
            destination_mode,
            fingerprint,
        )
        return {
            "path": arguments.path,
            "replacements": replacements,
            "bytes_written": len(encoded_content),
        }

    @staticmethod
    def _read_original(
        destination: Path,
    ) -> tuple[str, _FileFingerprint, int]:
        file_descriptor: int | None = None
        try:
            open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(destination, open_flags)
            with os.fdopen(file_descriptor, "rb") as existing_file:
                file_descriptor = None
                raw_content = existing_file.read(MAX_EDIT_FILE_BYTES + 1)
                file_stat = os.fstat(existing_file.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ToolExecutionError(
                        "The requested path is not a regular file.",
                        code="not_a_file",
                    )
        except ToolExecutionError:
            raise
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The requested file no longer exists.",
                code="file_not_found",
            ) from error
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while reading the file.",
                code="permission_denied",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The requested file could not be read.",
                code="read_failed",
            ) from error
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass

        if len(raw_content) > MAX_EDIT_FILE_BYTES:
            raise ToolExecutionError(
                f"The file exceeds the maximum size of {MAX_EDIT_FILE_BYTES} bytes.",
                code="file_too_large",
            )

        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "The requested file is not valid UTF-8 text.",
                code="unsupported_encoding",
            ) from error

        if any(
            (ord(character) < 0x20 and character not in _TEXT_CONTROL_CHARACTERS)
            or ord(character) == 0x7F
            for character in content
        ):
            raise ToolExecutionError(
                "The requested file is not supported UTF-8 text.",
                code="unsupported_encoding",
            )

        return (
            content,
            _FileFingerprint.from_stat(file_stat),
            stat.S_IMODE(file_stat.st_mode),
        )

    def _replace_atomically(
        self,
        destination: Path,
        requested_path: str,
        encoded_content: bytes,
        destination_mode: int,
        fingerprint: _FileFingerprint,
    ) -> None:
        temporary_fd: int | None = None
        temporary_path: Path | None = None
        replacement_succeeded = False

        try:
            temporary_fd, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)

            with os.fdopen(temporary_fd, "wb") as temporary_file:
                temporary_fd = None
                temporary_file.write(encoded_content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            revalidated_destination = resolve_existing_workspace_path(
                self._workspace_root,
                requested_path,
                expected="file",
            )
            if revalidated_destination != destination:
                raise ToolExecutionError(
                    "The file changed location while the edit was in progress.",
                    code="file_changed",
                )

            current_stat = revalidated_destination.lstat()
            if not stat.S_ISREG(current_stat.st_mode):
                raise ToolExecutionError(
                    "The file changed while the edit was in progress.",
                    code="file_changed",
                )
            current_fingerprint = _FileFingerprint.from_stat(current_stat)
            if current_fingerprint != fingerprint:
                raise ToolExecutionError(
                    "The file changed while the edit was in progress.",
                    code="file_changed",
                )

            os.chmod(temporary_path, destination_mode)
            sync_fd = os.open(temporary_path, os.O_RDONLY)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)

            os.replace(temporary_path, destination)
            replacement_succeeded = True
        except ToolExecutionError:
            raise
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while editing the file.",
                code="permission_denied",
            ) from error
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The file or its parent disappeared during the edit.",
                code="file_changed",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The file could not be edited.",
                code="edit_failed",
            ) from error
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary_path is not None and not replacement_succeeded:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
