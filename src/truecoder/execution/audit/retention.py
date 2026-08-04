from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

DEFAULT_RETENTION_DAYS: Final = 30
MIN_RETENTION_DAYS: Final = 1
MAX_RETENTION_DAYS: Final = 3650


class RetentionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    days: int = DEFAULT_RETENTION_DAYS
    keep_nonterminal: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.days, bool) or not isinstance(self.days, int):
            raise RetentionError("days must be an integer")
        if not MIN_RETENTION_DAYS <= self.days <= MAX_RETENTION_DAYS:
            raise RetentionError(
                f"days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
            )
        if not isinstance(self.keep_nonterminal, bool):
            raise RetentionError("keep_nonterminal must be a boolean")

    def cutoff(self, now: datetime | None = None) -> datetime:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            raise RetentionError("now must be timezone aware")
        return moment.astimezone(UTC) - timedelta(days=self.days)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    examined: int
    deleted: int
    retained_nonterminal: int
    cutoff_utc: datetime

    def __post_init__(self) -> None:
        for name in ("examined", "deleted", "retained_nonterminal"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RetentionError(f"{name} must be an integer")
            if value < 0:
                raise RetentionError(f"{name} cannot be negative")
        if self.deleted + self.retained_nonterminal > self.examined:
            raise RetentionError("deleted and retained cannot exceed examined")


def plan_retention(
    rows: tuple[tuple[str, datetime, bool], ...],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> tuple[tuple[str, ...], RetentionReport]:
    if not isinstance(rows, tuple):
        raise RetentionError("rows must be a tuple")
    if not isinstance(policy, RetentionPolicy):
        raise RetentionError("policy must be a RetentionPolicy")

    cutoff = policy.cutoff(now)
    deletable: list[str] = []
    retained_nonterminal = 0

    for entry in rows:
        run_id, updated_at, terminal = entry
        if not isinstance(run_id, str) or not run_id.strip():
            raise RetentionError("run identifiers cannot be empty")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise RetentionError("row timestamps must be timezone aware")
        if not isinstance(terminal, bool):
            raise RetentionError("row terminal flag must be a boolean")

        if updated_at.astimezone(UTC) >= cutoff:
            continue
        if not terminal and policy.keep_nonterminal:
            retained_nonterminal += 1
            continue
        deletable.append(run_id)

    return tuple(deletable), RetentionReport(
        examined=len(rows),
        deleted=len(deletable),
        retained_nonterminal=retained_nonterminal,
        cutoff_utc=cutoff,
    )
