from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from truecoder.tools.base import ToolCall

IDENTICAL_RESULT_THRESHOLD: Final = 3
CHANGING_RESULT_THRESHOLD: Final = 6


def canonical_call(call: ToolCall) -> str:
    try:
        arguments = json.loads(call.arguments_json)
    except (TypeError, ValueError):
        return f"{call.name}:{call.arguments_json}"
    return f"{call.name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IterationSignature:
    calls: tuple[str, ...]
    results: tuple[str, ...]

    @classmethod
    def create(
        cls,
        calls: Sequence[ToolCall],
        results: Sequence[str],
    ) -> IterationSignature:
        return cls(
            calls=tuple(canonical_call(call) for call in calls),
            results=tuple(digest(result) for result in results),
        )

    @property
    def described(self) -> str:
        names = []
        for entry in self.calls:
            name = entry.split(":", 1)[0]
            if name not in names:
                names.append(name)
        return ", ".join(names) or "the same tools"


class ProgressMonitor:
    def __init__(
        self,
        *,
        identical_threshold: int = IDENTICAL_RESULT_THRESHOLD,
        changing_threshold: int = CHANGING_RESULT_THRESHOLD,
    ) -> None:
        if identical_threshold < 2:
            raise ValueError("identical_threshold must be at least two")
        if changing_threshold < identical_threshold:
            raise ValueError("changing_threshold cannot be below identical_threshold")

        self._identical_threshold = identical_threshold
        self._changing_threshold = changing_threshold
        self._previous: IterationSignature | None = None
        self._call_repeats = 0
        self._result_repeats = 0

    @property
    def call_repeats(self) -> int:
        return self._call_repeats

    @property
    def result_repeats(self) -> int:
        return self._result_repeats

    def reset(self) -> None:
        self._previous = None
        self._call_repeats = 0
        self._result_repeats = 0

    def record(
        self,
        calls: Sequence[ToolCall],
        results: Sequence[str],
    ) -> str | None:
        if not calls:
            self.reset()
            return None

        signature = IterationSignature.create(calls, results)
        previous = self._previous
        self._previous = signature

        if previous is None or previous.calls != signature.calls:
            self._call_repeats = 1
            self._result_repeats = 1
            return None

        self._call_repeats += 1
        self._result_repeats = (
            self._result_repeats + 1 if previous.results == signature.results else 1
        )

        if self._result_repeats >= self._identical_threshold:
            return (
                f"You have called {signature.described} with identical arguments "
                f"{self._call_repeats} times and received an identical result each "
                "time. Repeating it cannot produce anything new. Answer now using "
                "what you already have, and say plainly what you could not "
                "determine."
            )

        if self._call_repeats >= self._changing_threshold:
            return (
                f"You have called {signature.described} with identical arguments "
                f"{self._call_repeats} times without reaching an answer. Answer now "
                "using what you already have, and say plainly what you could not "
                "determine."
            )

        return None
