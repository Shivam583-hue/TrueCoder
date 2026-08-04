from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

MAX_ROWS: Final = 200
MAX_PREVIEW_CHARS: Final = 2000
REDACTED: Final = "<redacted>"

TERMINAL_OUTCOMES: Final = (
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "denied",
    "limit_exceeded",
    "failed_to_start",
    "cleanup_failed",
    "recovery_failed",
)

_SENSITIVE_DETAIL_KEYS: Final = frozenset(
    {
        "api_key",
        "environment",
        "environment_values",
        "password",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class AuditFilter:
    outcome: str | None = None
    backend: str | None = None
    since_utc: datetime | None = None
    search: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is not None and self.outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"unknown audit outcome: {self.outcome!r}")
        if self.since_utc is not None:
            if not isinstance(self.since_utc, datetime):
                raise TypeError("since_utc must be a datetime")
            if self.since_utc.tzinfo is None:
                raise ValueError("since_utc must be timezone aware")
        if self.search is not None and len(self.search) > 200:
            raise ValueError("search text is too long")

    def matches(self, row: AuditRow) -> bool:
        if self.outcome is not None and row.outcome != self.outcome:
            return False
        if self.backend is not None and row.backend != self.backend:
            return False
        if self.since_utc is not None and row.updated_at_utc < self.since_utc:
            return False
        if self.search:
            needle = self.search.casefold()
            haystack = f"{row.command} {row.run_id} {row.audit_id}".casefold()
            if needle not in haystack:
                return False
        return True


@dataclass(frozen=True, slots=True)
class AuditRow:
    run_id: str
    audit_id: str
    command: str
    backend: str
    outcome: str
    exit_code: int | None
    updated_at_utc: datetime
    cleanup_complete: bool
    stdout_preview: str = ""
    stderr_preview: str = ""

    def __post_init__(self) -> None:
        for name in ("run_id", "audit_id", "backend", "outcome"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if not isinstance(self.updated_at_utc, datetime):
            raise TypeError("updated_at_utc must be a datetime")
        if self.updated_at_utc.tzinfo is None:
            raise ValueError("updated_at_utc must be timezone aware")
        if not isinstance(self.cleanup_complete, bool):
            raise TypeError("cleanup_complete must be a boolean")

    @property
    def terminal(self) -> bool:
        return self.outcome in TERMINAL_OUTCOMES

    def status_label(self) -> str:
        if not self.cleanup_complete:
            return f"{self.outcome} · cleanup incomplete"
        if self.exit_code is None:
            return self.outcome
        return f"{self.outcome} · exit {self.exit_code}"


def filter_rows(
    rows: tuple[AuditRow, ...],
    audit_filter: AuditFilter | None = None,
    *,
    limit: int = MAX_ROWS,
) -> tuple[AuditRow, ...]:
    if not isinstance(rows, tuple):
        raise TypeError("rows must be a tuple")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    selected = rows if audit_filter is None else tuple(
        row for row in rows if audit_filter.matches(row)
    )
    ordered = sorted(selected, key=lambda row: row.updated_at_utc, reverse=True)
    return tuple(ordered[: min(limit, MAX_ROWS)])


def sanitize_detail_rows(
    details: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(details, tuple):
        raise TypeError("details must be a tuple")
    sanitized: list[tuple[str, str]] = []
    for entry in details:
        name, value = entry
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("detail entries must be strings")
        lowered = name.strip().casefold().replace("-", "_")
        if any(marker in lowered for marker in _SENSITIVE_DETAIL_KEYS):
            sanitized.append((name, REDACTED))
            continue
        sanitized.append((name, _clamp(value)))
    return tuple(sanitized)


def bounded_preview(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _clamp(text)


def _clamp(value: str) -> str:
    if len(value) <= MAX_PREVIEW_CHARS:
        return value
    return f"{value[: MAX_PREVIEW_CHARS - 1]}…"


def summarize(rows: tuple[AuditRow, ...]) -> str:
    if not rows:
        return "No audit runs recorded"
    incomplete = sum(1 for row in rows if not row.cleanup_complete)
    nonterminal = sum(1 for row in rows if not row.terminal)
    parts = [f"{len(rows)} runs"]
    if incomplete:
        parts.append(f"{incomplete} with incomplete cleanup")
    if nonterminal:
        parts.append(f"{nonterminal} nonterminal")
    return " · ".join(parts)


def recent_cutoff(days: int, now: datetime | None = None) -> datetime:
    if isinstance(days, bool) or not isinstance(days, int):
        raise TypeError("days must be an integer")
    if days <= 0:
        raise ValueError("days must be greater than zero")
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).replace(microsecond=0) - _days(days)


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)
