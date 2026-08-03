from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

from truecoder.execution.errors import InvalidExecutionStateError
from truecoder.execution.models import (
    EXECUTION_STATUSES,
    LIMIT_TERMINATION_REASONS,
    TERMINATION_REASONS,
    ExecutionStatus,
    TerminationReason,
)


class RunState(str, Enum):
    ADMITTED = "admitted"
    POLICY_EVALUATED = "policy_evaluated"
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    TERMINATING = "terminating"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


_ALLOWED_TRANSITIONS: Final[dict[RunState, frozenset[RunState]]] = {
    RunState.ADMITTED: frozenset({RunState.POLICY_EVALUATED}),
    RunState.POLICY_EVALUATED: frozenset(
        {RunState.PREPARED, RunState.FINALIZING},
    ),
    RunState.PREPARED: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.REGISTERED},
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.REGISTERED, RunState.FINALIZING},
    ),
    RunState.REGISTERED: frozenset({RunState.STARTING}),
    RunState.STARTING: frozenset({RunState.RUNNING, RunState.FINALIZING}),
    RunState.RUNNING: frozenset({RunState.TERMINATING, RunState.FINALIZING}),
    RunState.TERMINATING: frozenset({RunState.FINALIZING}),
    RunState.FINALIZING: frozenset({RunState.TERMINAL}),
    RunState.TERMINAL: frozenset(),
}


TERMINAL_PRIORITY: Final = (
    "backend_exit",
    "output_limit",
    "resource_limit",
    "cancellation",
    "timeout",
)

_RANK_BY_SOURCE: Final[dict[str, int]] = {
    source: rank for rank, source in enumerate(TERMINAL_PRIORITY)
}

# An empty set means the status forbids a termination reason entirely.
_REASONS_BY_STATUS: Final[dict[ExecutionStatus, frozenset[str]]] = {
    "completed": frozenset(),
    "failed": frozenset(),
    "denied": frozenset(),
    "failed_to_start": frozenset(),
    "timed_out": frozenset({"timeout"}),
    "cancelled": frozenset({"cancellation", "shutdown"}),
    "limit_exceeded": LIMIT_TERMINATION_REASONS,
}


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} cannot be empty or whitespace-only")
    return value


def _require_choice(value: object, allowed: frozenset[str], name: str) -> str:
    text = _require_nonempty_string(value, name)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"unknown {name}: {text!r}; expected one of: {choices}")
    return text


def _require_finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _require_run_state(value: object, name: str) -> RunState:
    if not isinstance(value, RunState):
        raise TypeError(f"{name} must be a RunState")
    return value


@dataclass(frozen=True, slots=True)
class TerminalClaim:
    status: ExecutionStatus
    reason: TerminationReason | None
    observed_at_monotonic: float
    source: str

    def __post_init__(self) -> None:
        _require_choice(self.status, EXECUTION_STATUSES, "execution status")

        if self.reason is not None:
            _require_choice(self.reason, TERMINATION_REASONS, "termination reason")

        _require_finite_number(self.observed_at_monotonic, "observed_at_monotonic")

        source = _require_nonempty_string(self.source, "source")
        if any(character.isspace() for character in source):
            raise ValueError("source cannot contain whitespace")

        self._validate_status_invariants()

    def _validate_status_invariants(self) -> None:
        allowed = _REASONS_BY_STATUS[self.status]

        if not allowed:
            if self.reason is not None:
                raise ValueError(
                    f"a {self.status} claim cannot carry a termination reason"
                )
            return

        if self.reason not in allowed:
            choices = ", ".join(sorted(allowed))
            raise ValueError(
                f"a {self.status} claim requires one of these termination "
                f"reasons: {choices}"
            )


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    won: bool
    claim: TerminalClaim

    def __post_init__(self) -> None:
        if not isinstance(self.won, bool):
            raise TypeError("won must be a boolean")
        if not isinstance(self.claim, TerminalClaim):
            raise TypeError("claim must be a TerminalClaim")


@dataclass(frozen=True, slots=True)
class TerminalSettlement:
    claim: TerminalClaim
    finalized: bool
    failure: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim, TerminalClaim):
            raise TypeError("claim must be a TerminalClaim")
        if not isinstance(self.finalized, bool):
            raise TypeError("finalized must be a boolean")

        if self.finalized:
            if self.failure is not None:
                raise ValueError("a finalized settlement cannot carry a failure")
            return

        _require_nonempty_string(self.failure, "failure")

    @classmethod
    def recorded(cls, claim: TerminalClaim) -> TerminalSettlement:
        return cls(claim=claim, finalized=True)

    @classmethod
    def unrecorded(cls, claim: TerminalClaim, failure: str) -> TerminalSettlement:
        return cls(claim=claim, finalized=False, failure=failure)


def resolve_terminal_claim(candidates: tuple[TerminalClaim, ...]) -> TerminalClaim:
    if not isinstance(candidates, tuple):
        raise TypeError("candidates must be a tuple")
    if not candidates:
        raise ValueError("candidates cannot be empty")

    # Position is the last tie-breaker, so the caller's ordering decides only
    # when priority and observation time are both identical.
    ranked: list[tuple[int, float, int]] = []

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, TerminalClaim):
            raise TypeError(f"candidates[{index}] must be a TerminalClaim")

        rank = _RANK_BY_SOURCE.get(candidate.source)
        if rank is None:
            choices = ", ".join(TERMINAL_PRIORITY)
            raise ValueError(
                f"candidates[{index}] has an unrankable terminal source: "
                f"{candidate.source!r}; expected one of: {choices}"
            )

        ranked.append((rank, candidate.observed_at_monotonic, index))

    return candidates[min(ranked)[2]]


class LifecycleState:
    def __init__(self, execution_id: str) -> None:
        self._execution_id = _require_nonempty_string(
            execution_id,
            "execution_id",
        ).strip()
        self._current = RunState.ADMITTED
        self._history: list[RunState] = [RunState.ADMITTED]

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def current(self) -> RunState:
        return self._current

    @property
    def history(self) -> tuple[RunState, ...]:
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return self._current is RunState.TERMINAL

    def can_transition(self, target: RunState) -> bool:
        _require_run_state(target, "target")
        return target in _ALLOWED_TRANSITIONS[self._current]

    def transition(self, target: RunState) -> RunState:
        if not self.can_transition(target):
            raise InvalidExecutionStateError(
                f"cannot move from {self._current.value!r} to {target.value!r}",
                execution_id=self._execution_id,
                operation="lifecycle_transition",
            )

        self._current = target
        self._history.append(target)
        return target


class TerminalArbiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claim: TerminalClaim | None = None

    async def claim(self, candidate: TerminalClaim) -> ClaimOutcome:
        if not isinstance(candidate, TerminalClaim):
            raise TypeError("candidate must be a TerminalClaim")

        async with self._lock:
            if self._claim is None:
                self._claim = candidate
                return ClaimOutcome(won=True, claim=candidate)

            return ClaimOutcome(won=False, claim=self._claim)

    async def winner(self) -> TerminalClaim | None:
        async with self._lock:
            return self._claim
