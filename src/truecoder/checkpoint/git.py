from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GIT_TIMEOUT: Final = 30.0
CHECKPOINT_REF_PREFIX: Final = "refs/truecoder/checkpoints"
VERBATIM_CONTENT: Final = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
)


class GitUnavailableError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GitResult:
    status: int
    stdout_bytes: bytes
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == 0

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode("utf-8", errors="replace")


class GitWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        executable: str | None = None,
        timeout: float = GIT_TIMEOUT,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._root = root.resolve()
        self._executable = executable or shutil.which("git")
        self._timeout = timeout

    @property
    def root(self) -> Path:
        return self._root

    @property
    def executable(self) -> str | None:
        return self._executable

    async def unavailable_reason(self) -> str | None:
        if self._executable is None:
            return "git is not installed, so checkpoints are unavailable."
        result = await self._run("rev-parse", "--is-inside-work-tree")
        if not result.ok or result.stdout.strip() != "true":
            return "This workspace is not a git repository, so checkpoints are unavailable."
        return None

    async def available(self) -> bool:
        return await self.unavailable_reason() is None

    async def require(self) -> None:
        reason = await self.unavailable_reason()
        if reason is not None:
            raise GitUnavailableError(reason, code="checkpoints_unavailable")

    async def snapshot_tree(self) -> str:
        handle, index_path = tempfile.mkstemp(prefix="truecoder-index-")
        os.close(handle)
        os.unlink(index_path)

        try:
            staged = await self._run(
                "add",
                "--all",
                "--",
                ".",
                index=index_path,
            )
            if not staged.ok:
                raise GitUnavailableError(
                    f"The workspace could not be captured: {staged.stderr.strip()}",
                    code="snapshot_failed",
                )
            written = await self._run("write-tree", index=index_path)
            if not written.ok:
                raise GitUnavailableError(
                    f"The workspace tree could not be written: {written.stderr.strip()}",
                    code="snapshot_failed",
                )
            return written.stdout.strip()
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass

    async def commit_tree(self, tree: str, message: str) -> str:
        arguments = ["commit-tree", tree, "-m", message]
        head = await self.head_commit()
        if head is not None:
            arguments.extend(["-p", head])

        result = await self._run(*arguments)
        if not result.ok:
            raise GitUnavailableError(
                f"The checkpoint commit could not be created: {result.stderr.strip()}",
                code="snapshot_failed",
            )
        return result.stdout.strip()

    async def head_commit(self) -> str | None:
        result = await self._run("rev-parse", "--verify", "HEAD")
        return result.stdout.strip() if result.ok else None

    async def set_ref(self, ref: str, commit: str) -> None:
        result = await self._run("update-ref", ref, commit)
        if not result.ok:
            raise GitUnavailableError(
                f"The checkpoint reference could not be written: {result.stderr.strip()}",
                code="snapshot_failed",
            )

    async def delete_ref(self, ref: str) -> None:
        await self._run("update-ref", "-d", ref)

    async def list_refs(
        self, prefix: str = CHECKPOINT_REF_PREFIX
    ) -> tuple[tuple[str, str], ...]:
        result = await self._run(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            prefix,
        )
        if not result.ok:
            return ()

        entries: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            ref, _, commit = line.partition(" ")
            if ref and commit:
                entries.append((ref.strip(), commit.strip()))
        return tuple(entries)

    async def commit_message(self, commit: str) -> str:
        result = await self._run("log", "-1", "--format=%B", commit)
        return result.stdout if result.ok else ""

    async def tree_of(self, commit: str) -> str | None:
        result = await self._run("rev-parse", f"{commit}^{{tree}}")
        return result.stdout.strip() if result.ok else None

    async def paths_in_tree(self, tree: str) -> frozenset[str]:
        result = await self._run("ls-tree", "-r", "--name-only", tree)
        if not result.ok:
            return frozenset()
        return frozenset(line for line in result.stdout.splitlines() if line)

    async def tracked_paths(self) -> frozenset[str]:
        result = await self._run("ls-files")
        if not result.ok:
            return frozenset()
        return frozenset(line for line in result.stdout.splitlines() if line)

    async def changed_paths(
        self,
        tree: str,
        current: str,
    ) -> tuple[tuple[str, str], ...]:
        result = await self._run(
            "diff-tree",
            "-r",
            "--name-status",
            "--no-renames",
            "-z",
            tree,
            current,
        )
        if not result.ok:
            return ()

        fields = [field for field in result.stdout.split("\0") if field]
        return tuple(
            (fields[index], fields[index + 1]) for index in range(0, len(fields) - 1, 2)
        )

    async def blob_size(self, tree: str, path: str) -> int | None:
        result = await self._run("cat-file", "-s", f"{tree}:{path}")
        if not result.ok:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    async def blob_at(self, tree: str, path: str) -> bytes | None:
        result = await self._run("show", f"{tree}:{path}")
        return result.stdout_bytes if result.ok else None

    async def restore_tree(self, tree: str) -> None:
        result = await self._run("read-tree", "-u", "--reset", tree)
        if not result.ok:
            raise GitUnavailableError(
                f"The workspace could not be restored: {result.stderr.strip()}",
                code="restore_failed",
            )

    async def _run(self, *arguments: str, index: str | None = None) -> GitResult:
        if self._executable is None:
            raise GitUnavailableError(
                "git is not installed, so checkpoints are unavailable.",
                code="checkpoints_unavailable",
            )

        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if index is not None:
            environment["GIT_INDEX_FILE"] = index

        return await self._execute(
            (
                self._executable,
                *VERBATIM_CONTENT,
                "-C",
                str(self._root),
                *arguments,
            ),
            environment,
        )

    async def _execute(
        self,
        command: Sequence[str],
        environment: dict[str, str],
    ) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
                env=environment,
            )
        except OSError as error:
            raise GitUnavailableError(
                f"git could not be started: {error}",
                code="checkpoints_unavailable",
            ) from error

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as error:
            process.kill()
            await process.wait()
            raise GitUnavailableError(
                "git did not finish in time.",
                code="git_timeout",
            ) from error

        return GitResult(
            status=process.returncode or 0,
            stdout_bytes=stdout,
            stderr=stderr.decode("utf-8", errors="replace"),
        )
