from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

from truecoder.checkpoint.changes import WorkspaceChanges, compute_changes
from truecoder.checkpoint.git import (
    CHECKPOINT_REF_PREFIX,
    GitUnavailableError,
    GitWorkspace,
)
from truecoder.checkpoint.models import (
    Checkpoint,
    RestoreOutcome,
    decode_message,
    encode_message,
    normalize_label,
    now_utc,
)

MAX_CHECKPOINTS: Final = 25
SAFETY_LABEL: Final = "before restore"


class CheckpointService:
    def __init__(
        self,
        workspace: GitWorkspace,
        *,
        limit: int = MAX_CHECKPOINTS,
    ) -> None:
        if not isinstance(workspace, GitWorkspace):
            raise TypeError("workspace must be a GitWorkspace")
        if limit < 1:
            raise ValueError("limit must be at least one")

        self._workspace = workspace
        self._limit = limit

    @property
    def root(self) -> Path:
        return self._workspace.root

    async def unavailable_reason(self) -> str | None:
        return await self._workspace.unavailable_reason()

    async def capture(
        self,
        label: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        skip_unchanged: bool = True,
    ) -> Checkpoint | None:
        await self._workspace.require()

        tree = await self._workspace.snapshot_tree()
        existing = await self.list()

        if skip_unchanged and existing and existing[0].tree == tree:
            return existing[0]

        checkpoint_id = (
            f"{now_utc().replace(':', '').replace('.', '')}-{uuid.uuid4().hex[:8]}"
        )
        created_at = now_utc()
        message = encode_message(
            label=label,
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            session_id=session_id,
            turn_id=turn_id,
        )
        commit = await self._workspace.commit_tree(tree, message)
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            commit=commit,
            tree=tree,
            label=normalize_label(label),
            created_at=created_at,
            session_id=session_id,
            turn_id=turn_id,
        )
        await self._workspace.set_ref(checkpoint.ref, commit)
        await self._prune(existing)
        return checkpoint

    async def list(self) -> tuple[Checkpoint, ...]:
        entries = await self._workspace.list_refs(CHECKPOINT_REF_PREFIX)
        checkpoints: list[Checkpoint] = []

        for ref, commit in entries:
            metadata = decode_message(await self._workspace.commit_message(commit))
            tree = await self._workspace.tree_of(commit)
            if tree is None:
                continue
            checkpoints.append(
                Checkpoint(
                    checkpoint_id=str(metadata.get("id") or ref.rsplit("/", 1)[-1]),
                    commit=commit,
                    tree=tree,
                    label=str(metadata.get("label") or "untitled"),
                    created_at=str(metadata.get("created_at") or ""),
                    session_id=str(metadata.get("session_id") or ""),
                    turn_id=str(metadata.get("turn_id") or ""),
                )
            )

        return tuple(
            sorted(checkpoints, key=lambda entry: entry.created_at, reverse=True)
        )

    async def find(self, checkpoint_id: str) -> Checkpoint | None:
        for checkpoint in await self.list():
            if checkpoint.checkpoint_id == checkpoint_id:
                return checkpoint
        return None

    async def preview(self, checkpoint: Checkpoint) -> tuple[str, ...]:
        tracked = await self._workspace.tracked_paths()
        restored = await self._workspace.paths_in_tree(checkpoint.tree)
        return tuple(sorted(tracked - restored))

    async def changes_since(self, checkpoint: Checkpoint) -> WorkspaceChanges:
        await self._workspace.require()
        return await compute_changes(self._workspace, checkpoint)

    async def latest_changes(self) -> WorkspaceChanges | None:
        existing = await self.list()
        if not existing:
            return None
        return await self.changes_since(existing[0])

    async def restore(self, checkpoint_id: str) -> RestoreOutcome:
        await self._workspace.require()

        checkpoint = await self.find(checkpoint_id)
        if checkpoint is None:
            raise GitUnavailableError(
                "That checkpoint no longer exists.",
                code="checkpoint_not_found",
            )

        safety = await self.capture(
            SAFETY_LABEL,
            session_id=checkpoint.session_id,
            skip_unchanged=False,
        )

        before = await self._workspace.tracked_paths()
        await self._workspace.restore_tree(checkpoint.tree)
        after = await self._workspace.tracked_paths()

        return RestoreOutcome(
            checkpoint=checkpoint,
            safety=safety,
            removed=tuple(sorted(before - after)),
            kept_untracked=(),
        )

    async def _prune(self, existing: tuple[Checkpoint, ...]) -> None:
        surplus = list(existing)[self._limit - 1 :]
        for checkpoint in surplus:
            await self._workspace.delete_ref(checkpoint.ref)
