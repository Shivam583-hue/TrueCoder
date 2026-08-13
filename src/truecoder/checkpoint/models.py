from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from truecoder._compat import UTC

METADATA_MARKER: Final = "truecoder-checkpoint:"
MAX_LABEL_LENGTH: Final = 120


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    commit: str
    tree: str
    label: str
    created_at: str
    session_id: str = ""
    turn_id: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_id.strip():
            raise ValueError("A checkpoint requires an identifier.")
        if not self.commit.strip():
            raise ValueError("A checkpoint requires a commit.")
        if not self.tree.strip():
            raise ValueError("A checkpoint requires a tree.")

        object.__setattr__(self, "label", normalize_label(self.label))

    @property
    def ref(self) -> str:
        return f"refs/truecoder/checkpoints/{self.checkpoint_id}"

    @property
    def moment(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.created_at)
        except ValueError:
            return None


def normalize_label(label: str) -> str:
    if not isinstance(label, str):
        raise TypeError("A checkpoint label must be text.")
    collapsed = " ".join(label.split())
    if not collapsed:
        return "untitled"
    return collapsed[:MAX_LABEL_LENGTH]


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def encode_message(
    *,
    label: str,
    checkpoint_id: str,
    created_at: str,
    session_id: str,
    turn_id: str,
) -> str:
    metadata = {
        "id": checkpoint_id,
        "created_at": created_at,
        "session_id": session_id,
        "turn_id": turn_id,
        "label": normalize_label(label),
    }
    return (
        f"TrueCoder checkpoint: {normalize_label(label)}\n\n"
        f"{METADATA_MARKER} {json.dumps(metadata, sort_keys=True)}\n"
    )


def decode_message(message: str) -> dict[str, Any]:
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped.startswith(METADATA_MARKER):
            continue
        payload = stripped[len(METADATA_MARKER) :].strip()
        try:
            decoded = json.loads(payload)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    checkpoint: Checkpoint
    safety: Checkpoint | None
    removed: tuple[str, ...]
    kept_untracked: tuple[str, ...]

    @property
    def summary(self) -> str:
        parts = [f"Restored {self.checkpoint.label}"]
        if self.removed:
            parts.append(f"{len(self.removed)} file(s) removed")
        if self.kept_untracked:
            parts.append(f"{len(self.kept_untracked)} untracked file(s) left in place")
        return "  ·  ".join(parts)
