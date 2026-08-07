from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from truecoder.checkpoint.git import GitWorkspace
from truecoder.checkpoint.models import Checkpoint
from truecoder.mutation import FileDiff, build_file_diff

MAX_CHANGED_FILES: Final = 50
MAX_DIFFED_BYTES: Final = 1024 * 1024

ChangeKind = Literal["added", "modified", "deleted"]

_STATUS_KINDS: Final[dict[str, ChangeKind]] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "modified",
    "U": "modified",
}


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: ChangeKind
    diff: FileDiff | None

    @property
    def added(self) -> int:
        return 0 if self.diff is None else self.diff.added

    @property
    def removed(self) -> int:
        return 0 if self.diff is None else self.diff.removed

    @property
    def summary(self) -> str:
        if self.diff is None:
            return f"{self.kind}, not shown as text"
        return f"{self.kind}  +{self.added}  -{self.removed}"


@dataclass(frozen=True, slots=True)
class WorkspaceChanges:
    label: str
    changes: tuple[FileChange, ...]
    total: int = 0

    def __post_init__(self) -> None:
        if self.total < len(self.changes):
            object.__setattr__(self, "total", len(self.changes))

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def truncated(self) -> bool:
        return self.total > len(self.changes)

    @property
    def added(self) -> int:
        return sum(change.added for change in self.changes)

    @property
    def removed(self) -> int:
        return sum(change.removed for change in self.changes)

    @property
    def summary(self) -> str:
        if self.is_empty:
            return "No files changed"

        parts = [f"{self.total} file(s) changed"]
        if self.added or self.removed:
            parts.append(f"+{self.added}  -{self.removed}")
        if self.truncated:
            parts.append(f"showing {len(self.changes)}")
        return "  ·  ".join(parts)


def _decode(payload: bytes | None) -> str | None:
    if payload is None or len(payload) > MAX_DIFFED_BYTES:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def _side(
    workspace: GitWorkspace,
    tree: str,
    path: str,
    *,
    absent: bool,
) -> str | None:
    if absent:
        return ""

    size = await workspace.blob_size(tree, path)
    if size is None or size > MAX_DIFFED_BYTES:
        return None
    return _decode(await workspace.blob_at(tree, path))


async def compute_changes(
    workspace: GitWorkspace,
    checkpoint: Checkpoint,
    *,
    max_files: int = MAX_CHANGED_FILES,
) -> WorkspaceChanges:
    if max_files < 1:
        raise ValueError("max_files must be at least one")

    current = await workspace.snapshot_tree()
    entries = await workspace.changed_paths(checkpoint.tree, current)
    changes: list[FileChange] = []

    for status, path in entries[:max_files]:
        kind = _STATUS_KINDS.get(status[:1], "modified")
        before = await _side(
            workspace,
            checkpoint.tree,
            path,
            absent=kind == "added",
        )
        after = await _side(workspace, current, path, absent=kind == "deleted")

        if before is None or after is None:
            changes.append(FileChange(path=path, kind=kind, diff=None))
            continue

        changes.append(
            FileChange(
                path=path,
                kind=kind,
                diff=build_file_diff(
                    path,
                    before,
                    after,
                    kind="create" if kind == "added" else "replace",
                ),
            )
        )

    return WorkspaceChanges(
        label=checkpoint.label,
        changes=tuple(changes),
        total=len(entries),
    )
