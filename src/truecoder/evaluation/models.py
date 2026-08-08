from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MAX_DETAIL_CHARACTERS: Final = 400

Check = Callable[[Path], "str | None"]


@dataclass(frozen=True, slots=True)
class EvalTask:
    name: str
    prompt: str
    files: Mapping[str, str] = field(default_factory=dict)
    check: Check | None = None
    max_iterations: int = 12

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a task needs a name")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError(f"task {self.name!r} needs a prompt")
        if self.check is not None and not callable(self.check):
            raise TypeError(f"task {self.name!r} check must be callable")
        if self.max_iterations < 1:
            raise ValueError(f"task {self.name!r} needs at least one iteration")


@dataclass(frozen=True, slots=True)
class EvalResult:
    task: str
    passed: bool
    detail: str | None = None
    reply: str = ""
    tool_calls: int = 0

    @property
    def summary(self) -> str:
        mark = "pass" if self.passed else "FAIL"
        suffix = f"  {self.detail}" if self.detail else ""
        return f"{mark}  {self.task}{suffix}"


@dataclass(frozen=True, slots=True)
class EvalReport:
    results: tuple[EvalResult, ...] = ()

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def is_clean(self) -> bool:
        return self.total > 0 and self.passed == self.total

    @property
    def summary(self) -> str:
        return f"{self.passed}/{self.total} tasks passed"


def file_contains(path: str, expected: str) -> Check:
    def check(root: Path) -> str | None:
        target = root / path
        if not target.is_file():
            return f"{path} does not exist"
        content = target.read_text(encoding="utf-8", errors="replace")
        if expected not in content:
            return f"{path} does not contain {expected!r}"
        return None

    return check


def file_unchanged(path: str, expected: str) -> Check:
    def check(root: Path) -> str | None:
        target = root / path
        if not target.is_file():
            return f"{path} was removed"
        content = target.read_text(encoding="utf-8", errors="replace")
        if content != expected:
            return f"{path} was modified"
        return None

    return check


def all_of(*checks: Check) -> Check:
    def check(root: Path) -> str | None:
        for candidate in checks:
            failure = candidate(root)
            if failure is not None:
                return failure
        return None

    return check
