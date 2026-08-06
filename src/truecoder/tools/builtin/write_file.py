from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from typing import TypedDict

from pydantic import Field

from truecoder.mutation import FileDiff, build_file_diff
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.builtin.filesystem import is_sensitive_path
from truecoder.tools.context import ToolInvocationContext
from truecoder.tools.mutation_audit import MutationAudit, record_mutation

MAX_WRITE_BYTES = 32 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024

# --------------------------------------------------------------------


class WriteFileArguments(ToolArguments):
    """Validated arguments accepted by the write-file tool."""

    path: str = Field(
        min_length=1,
        description="Path to the file, relative to the workspace.",
    )
    content: str = Field(
        description="Complete UTF-8 text content to write to the file.",
    )


# --------------------------------------------------------------------


class WriteFileOutput(TypedDict):
    """Structured output returned by the write-file tool."""

    path: str
    created: bool
    bytes_written: int


# --------------------------------------------------------------------


class WriteFileTool(BaseTool[WriteFileArguments]):
    """Atomically write bounded UTF-8 text files in a trusted workspace."""

    name = "write_file"
    description = (
        "Create or completely replace a UTF-8 text file inside the workspace. "
        "The parent directory must already exist."
    )
    arguments_type = WriteFileArguments
    approval = ToolApproval.REQUIRED

    # -------------------------------

    def __init__(
        self,
        workspace_root: Path,
        mutation_audit: MutationAudit | None = None,
    ) -> None:
        if not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be a pathlib.Path.")

        if mutation_audit is not None and not isinstance(mutation_audit, MutationAudit):
            raise TypeError("mutation_audit must be a MutationAudit or None")

        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be an absolute path.")

        try:
            resolved_root = workspace_root.resolve(strict=True)
        except OSError as error:
            raise ValueError("workspace_root must exist and be accessible.") from error

        if not resolved_root.is_dir():
            raise ValueError("workspace_root must be a directory.")

        self._workspace_root = resolved_root
        self._mutation_audit = mutation_audit

    # -------------------------------

    @property
    def workspace_root(self) -> Path:
        """Return the trusted, resolved workspace root."""

        return self._workspace_root

    @property
    def mutation_audit(self) -> MutationAudit | None:
        return self._mutation_audit

    def _write_atomic(
        self,
        requested_path: str,
        encoded_content: bytes,
        invocation: ToolInvocationContext | None = None,
    ) -> WriteFileOutput:
        """Synchronously write content using an atomic replacement."""

        destination, is_new_file = self._resolve_destination(requested_path)
        original_bytes = (
            None
            if is_new_file or self._mutation_audit is None
            else self._read_for_audit(destination)
        )

        temporary_path: Path | None = None
        temporary_fd: int | None = None
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

            revalidated_destination, created = self._resolve_destination(requested_path)

            if revalidated_destination != destination:
                raise ToolExecutionError(
                    "The destination changed while the write was in progress.",
                    code="outside_workspace",
                )

            if created:
                destination_mode = 0o644
            else:
                try:
                    destination_stat = destination.lstat()
                except FileNotFoundError as error:
                    raise ToolExecutionError(
                        "The destination changed while the write was in progress.",
                        code="write_failed",
                    ) from error
                except PermissionError as error:
                    raise ToolExecutionError(
                        "Permission was denied while inspecting the destination.",
                        code="permission_denied",
                    ) from error
                except OSError as error:
                    raise ToolExecutionError(
                        "The destination could not be inspected before writing.",
                        code="write_failed",
                    ) from error

                if stat.S_ISLNK(destination_stat.st_mode):
                    raise ToolExecutionError(
                        "Writing to symbolic links is not allowed.",
                        code="symlink_not_allowed",
                    )

                if not stat.S_ISREG(destination_stat.st_mode):
                    raise ToolExecutionError(
                        "The destination is not a regular file.",
                        code="not_a_file",
                    )

                destination_mode = stat.S_IMODE(destination_stat.st_mode)

            sync_fd = os.open(temporary_path, os.O_RDWR)
            try:
                os.chmod(temporary_path, destination_mode)
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)

            os.replace(temporary_path, destination)
            replacement_succeeded = True

            record_mutation(
                self._mutation_audit,
                invocation,
                tool_name=self.name,
                path=requested_path,
                kind="create" if created else "replace",
                before=original_bytes,
                after=encoded_content,
                diff=self._audit_diff(original_bytes, encoded_content, requested_path),
            )

            return {
                "path": requested_path,
                "created": created,
                "bytes_written": len(encoded_content),
            }

        except ToolExecutionError:
            raise
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while writing the file.",
                code="permission_denied",
            ) from error
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The destination parent directory no longer exists.",
                code="parent_not_found",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The file could not be written.",
                code="write_failed",
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

    # -------------------------------

    async def run(
        self,
        arguments: WriteFileArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> WriteFileOutput:
        """Write a bounded UTF-8 text file atomically.

        Once dispatched to the worker thread, cancellation cannot guarantee
        rollback because the atomic replacement may already have completed.
        """

        if not isinstance(arguments.content, str):
            raise ToolExecutionError(
                "Only text content is supported.",
                code="unsupported_content",
            )

        # Permit:
        #   U+0009: horizontal tab
        #   U+000A: line feed
        #   U+000D: carriage return
        for character in arguments.content:
            code_point = ord(character)

            is_disallowed_c0 = code_point < 0x20 and character not in ("\t", "\n", "\r")
            is_delete = code_point == 0x7F

            if is_disallowed_c0 or is_delete:
                raise ToolExecutionError(
                    (
                        "Content contains an unsupported control character: "
                        f"U+{code_point:04X}."
                    ),
                    code="unsupported_content",
                )

        try:
            encoded_content = arguments.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolExecutionError(
                "The content could not be encoded as UTF-8.",
                code="unsupported_content",
            ) from error

        if len(encoded_content) > MAX_WRITE_BYTES:
            raise ToolExecutionError(
                (f"Content exceeds the maximum size of {MAX_WRITE_BYTES} bytes."),
                code="content_too_large",
            )

        return await asyncio.to_thread(
            self._write_atomic,
            arguments.path,
            encoded_content,
            invocation,
        )

    # -------------------------------

    @staticmethod
    def _read_for_audit(destination: Path) -> bytes | None:
        try:
            return destination.read_bytes()
        except OSError:
            return None

    @staticmethod
    def _audit_diff(
        original_bytes: bytes | None,
        encoded_content: bytes,
        requested_path: str,
    ) -> FileDiff | None:
        try:
            before = "" if original_bytes is None else original_bytes.decode("utf-8")
            after = encoded_content.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return build_file_diff(
            requested_path,
            before,
            after,
            kind="create" if original_bytes is None else "replace",
        )

    async def preview_mutation(self, arguments: WriteFileArguments) -> FileDiff | None:
        return await asyncio.to_thread(self._preview, arguments)

    def _preview(self, arguments: WriteFileArguments) -> FileDiff | None:
        destination, created = self._resolve_destination(arguments.path)

        if created:
            return build_file_diff(
                arguments.path,
                "",
                arguments.content,
                kind="create",
            )

        try:
            raw_content = destination.read_bytes()
        except OSError:
            return None

        if len(raw_content) > MAX_PREVIEW_BYTES:
            return None

        try:
            original_content = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            return None

        return build_file_diff(
            arguments.path,
            original_content,
            arguments.content,
            kind="replace",
        )

    # -------------------------------

    def _resolve_destination(self, requested_path: str) -> tuple[Path, bool]:
        requested = Path(requested_path)

        # 1. Reject absolute paths.
        if requested.is_absolute():
            raise ToolExecutionError(
                "Absolute paths cannot be written.",
                code="outside_workspace",
            )

        # 2. Build the target path and resolve its parent.
        target_path = self._workspace_root / requested
        parent_dir = target_path.parent

        # Reject symbolic links before resolving the parent so links outside the
        # workspace are reported consistently and are never followed.
        current_path = parent_dir
        while (
            current_path != self._workspace_root and current_path != current_path.parent
        ):
            try:
                if current_path.is_symlink():
                    raise ToolExecutionError(
                        "Symbolic links are not allowed in the destination path.",
                        code="symlink_not_allowed",
                    )
            except PermissionError as error:
                raise ToolExecutionError(
                    "Permission was denied while inspecting the destination path.",
                    code="permission_denied",
                ) from error
            except OSError as error:
                raise ToolExecutionError(
                    "The destination path could not be inspected safely.",
                    code="outside_workspace",
                ) from error
            current_path = current_path.parent

        try:
            resolved_parent = parent_dir.resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The parent directory does not exist.",
                code="parent_not_found",
            ) from error
        except NotADirectoryError as error:
            raise ToolExecutionError(
                "The parent path is not a directory.",
                code="not_a_directory",
            ) from error
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while resolving the parent directory.",
                code="permission_denied",
            ) from error
        except (OSError, RuntimeError) as error:
            raise ToolExecutionError(
                "The parent directory could not be resolved safely.",
                code="outside_workspace",
            ) from error

        # 3. Ensure the resolved parent remains inside the workspace.
        if not resolved_parent.is_relative_to(self._workspace_root):
            raise ToolExecutionError(
                "The parent directory is outside the workspace.",
                code="outside_workspace",
            )

        # 4. Ensure the parent is a directory.
        try:
            parent_stat = resolved_parent.stat()
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while inspecting the parent directory.",
                code="permission_denied",
            ) from error
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "The parent directory no longer exists.",
                code="parent_not_found",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The parent directory could not be inspected.",
                code="parent_not_found",
            ) from error

        if not stat.S_ISDIR(parent_stat.st_mode):
            raise ToolExecutionError(
                "The parent path is not a directory.",
                code="not_a_directory",
            )

        # 5. Build and validate the final destination.
        final_destination = resolved_parent / target_path.name

        if not final_destination.is_relative_to(self._workspace_root):
            raise ToolExecutionError(
                "The requested path is outside the workspace.",
                code="outside_workspace",
            )

        # 6. Reject sensitive paths.
        workspace_path = final_destination.relative_to(self._workspace_root)

        if is_sensitive_path(workspace_path):
            raise ToolExecutionError(
                "The requested path is considered sensitive.",
                code="sensitive_path",
            )

        # 7. Inspect an existing target.
        try:
            target_stat = final_destination.lstat()
        except FileNotFoundError:
            return final_destination, True
        except PermissionError as error:
            raise ToolExecutionError(
                "Permission was denied while accessing the target path.",
                code="permission_denied",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "The target path could not be inspected.",
                code="write_failed",
            ) from error
        else:
            if stat.S_ISLNK(target_stat.st_mode):
                raise ToolExecutionError(
                    "Writing to symbolic links is not allowed.",
                    code="symlink_not_allowed",
                )

            if stat.S_ISDIR(target_stat.st_mode):
                raise ToolExecutionError(
                    "The target path is a directory.",
                    code="not_a_file",
                )

            if not stat.S_ISREG(target_stat.st_mode):
                raise ToolExecutionError(
                    "The target path is not a regular file.",
                    code="not_a_file",
                )

            return final_destination, False

    # -------------------------------
