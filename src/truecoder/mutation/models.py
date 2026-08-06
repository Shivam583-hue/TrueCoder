from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

MutationKind: TypeAlias = Literal["create", "replace", "edit"]
DiffLineKind: TypeAlias = Literal["context", "added", "removed"]

MUTATION_KINDS: Final[tuple[MutationKind, ...]] = ("create", "replace", "edit")
DIFF_LINE_KINDS: Final[tuple[DiffLineKind, ...]] = ("context", "added", "removed")

DIFF_CONTEXT_LINES: Final = 3
MAX_DIFF_LINES: Final = 400
MAX_DIFF_LINE_LENGTH: Final = 500

DIFF_LINE_PREFIXES: Final[dict[DiffLineKind, str]] = {
    "context": " ",
    "added": "+",
    "removed": "-",
}


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: DiffLineKind
    text: str
    before_number: int | None
    after_number: int | None

    def __post_init__(self) -> None:
        if self.kind not in DIFF_LINE_KINDS:
            raise ValueError(f"Unsupported diff line kind: {self.kind!r}")
        if not isinstance(self.text, str):
            raise TypeError("A diff line must carry text.")
        if self.kind == "added" and self.before_number is not None:
            raise ValueError("An added line has no line number in the original.")
        if self.kind == "removed" and self.after_number is not None:
            raise ValueError("A removed line has no line number in the result.")


@dataclass(frozen=True, slots=True)
class DiffHunk:
    before_start: int
    before_count: int
    after_start: int
    after_count: int
    lines: tuple[DiffLine, ...]

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("A diff hunk requires at least one line.")
        if not all(isinstance(line, DiffLine) for line in self.lines):
            raise TypeError("A diff hunk may contain only DiffLine values.")
        for name in ("before_start", "before_count", "after_start", "after_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer.")

    @property
    def header(self) -> str:
        return (
            f"@@ -{self.before_start},{self.before_count} "
            f"+{self.after_start},{self.after_count} @@"
        )


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    kind: MutationKind
    hunks: tuple[DiffHunk, ...]
    added: int
    removed: int
    truncated: bool = False
    newline_changed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("A file diff requires a path.")
        if self.kind not in MUTATION_KINDS:
            raise ValueError(f"Unsupported mutation kind: {self.kind!r}")
        if not all(isinstance(hunk, DiffHunk) for hunk in self.hunks):
            raise TypeError("A file diff may contain only DiffHunk values.")
        if self.added < 0 or self.removed < 0:
            raise ValueError("Diff line counts cannot be negative.")

    @property
    def is_empty(self) -> bool:
        return not self.hunks and not self.newline_changed

    @property
    def summary(self) -> str:
        parts = [f"+{self.added}", f"-{self.removed}"]
        if self.truncated:
            parts.append("truncated")
        return "  ".join(parts)
