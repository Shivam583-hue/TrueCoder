from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from truecoder.execution.audit.models import (
    AuditEvent,
    AuditRunSnapshot,
    TerminalOutcome,
)

MAX_ROWS: Final = 200
MAX_PREVIEW_CHARS: Final = 2000
REDACTED: Final = "<redacted>"

TERMINAL_OUTCOMES: Final = tuple(outcome.value for outcome in TerminalOutcome)
VISIBLE_OUTCOMES: Final = ("pending", "running", *TERMINAL_OUTCOMES)

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
        if self.outcome is not None and self.outcome not in VISIBLE_OUTCOMES:
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
            haystack = f"{row.command} {row.run_id}".casefold()
            if needle not in haystack:
                return False
        return True


@dataclass(frozen=True, slots=True)
class AuditRow:
    run_id: str
    command: str
    backend: str
    outcome: str
    exit_code: int | None
    updated_at_utc: datetime
    cleanup_complete: bool
    stdout_preview: str = ""
    stderr_preview: str = ""
    detail: str | None = None
    event_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "backend", "outcome"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if self.outcome not in VISIBLE_OUTCOMES:
            raise ValueError(f"unknown audit outcome: {self.outcome!r}")
        if not isinstance(self.command, str):
            raise TypeError("command must be a string")
        if not isinstance(self.updated_at_utc, datetime):
            raise TypeError("updated_at_utc must be a datetime")
        if self.updated_at_utc.tzinfo is None:
            raise ValueError("updated_at_utc must be timezone aware")
        if not isinstance(self.cleanup_complete, bool):
            raise TypeError("cleanup_complete must be a boolean")
        if self.detail is not None and not isinstance(self.detail, str):
            raise TypeError("detail must be a string or None")
        if not isinstance(self.event_lines, tuple) or any(
            not isinstance(line, str) for line in self.event_lines
        ):
            raise TypeError("event_lines must be a tuple of strings")

    @property
    def audit_id(self) -> str:
        return self.run_id

    @property
    def terminal(self) -> bool:
        return self.outcome in TERMINAL_OUTCOMES

    def status_label(self) -> str:
        if not self.cleanup_complete:
            return f"{self.outcome} · cleanup incomplete"
        if self.exit_code is None:
            return self.outcome
        return f"{self.outcome} · exit {self.exit_code}"

    def details_text(self) -> str:
        lines = [
            self.command or "(command unavailable)",
            "",
            f"Run       {self.run_id}",
            f"Backend   {self.backend}",
            f"Status    {self.status_label()}",
            f"Updated   {self.updated_at_utc.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if self.detail:
            lines.extend(("", f"Detail    {bounded_preview(self.detail)}"))
        if self.stdout_preview:
            lines.extend(("", "stdout", bounded_preview(self.stdout_preview)))
        if self.stderr_preview:
            lines.extend(("", "stderr", bounded_preview(self.stderr_preview)))
        if self.event_lines:
            lines.extend(("", "Lifecycle", *self.event_lines))
        return "\n".join(lines)


def audit_row_from(
    snapshot: AuditRunSnapshot,
    events: tuple[AuditEvent, ...] = (),
) -> AuditRow:
    if not isinstance(snapshot, AuditRunSnapshot):
        raise TypeError("snapshot must be an AuditRunSnapshot")
    if not isinstance(events, tuple) or any(
        not isinstance(event, AuditEvent) for event in events
    ):
        raise TypeError("events must be a tuple of AuditEvent values")

    summary = dict(snapshot.admission.request_summary)
    finalization = snapshot.record.finalization
    outcome = (
        finalization.outcome.value
        if finalization is not None
        else snapshot.record.phase.value
    )
    output = finalization.output if finalization is not None else None
    resource = snapshot.resource or (
        finalization.resource if finalization is not None else None
    )
    backend = resource.backend if resource is not None else summary.get(
        "backend",
        "auto",
    )
    return AuditRow(
        run_id=snapshot.record.run_id,
        command=summary.get("command", ""),
        backend=backend,
        outcome=outcome,
        exit_code=finalization.exit_code if finalization is not None else None,
        updated_at_utc=snapshot.record.updated_at,
        cleanup_complete=outcome != TerminalOutcome.CLEANUP_FAILED.value,
        stdout_preview=output.stdout_preview if output is not None else "",
        stderr_preview=output.stderr_preview if output is not None else "",
        detail=finalization.detail if finalization is not None else None,
        event_lines=tuple(_event_line(event) for event in events),
    )


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
    return moment.astimezone(UTC).replace(microsecond=0) - timedelta(days=days)


class AuditListItem(ListItem):
    def __init__(self, row: AuditRow) -> None:
        self.row = row
        super().__init__()

    def compose(self) -> ComposeResult:
        updated = self.row.updated_at_utc.astimezone().strftime("%m-%d %H:%M")
        yield Label(
            f"{self.row.status_label()}  {updated}\n{self.row.command}",
            markup=False,
        )


class AuditViewerScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("f", "cycle_outcome", "Outcome", show=False),
        Binding("b", "cycle_backend", "Backend", show=False),
        Binding("r", "cycle_recency", "Recency", show=False),
        Binding("slash", "focus_search", "Search", show=False),
    ]

    def __init__(self, rows: tuple[AuditRow, ...]) -> None:
        self.rows = filter_rows(rows)
        self._outcomes = (None, *sorted({row.outcome for row in self.rows}))
        self._backends = (None, *sorted({row.backend for row in self.rows}))
        self._recency_days: tuple[int | None, ...] = (None, 1, 7, 30)
        self._outcome_index = 0
        self._backend_index = 0
        self._recency_index = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="audit-viewer-dialog"):
            yield Static("Execution audit", classes="audit-title")
            yield Input(
                placeholder="Filter by command or run ID",
                id="audit-search",
                max_length=200,
            )
            yield Static("", id="audit-filter-summary")
            with Horizontal(id="audit-content"):
                yield ListView(
                    *(AuditListItem(row) for row in self.rows),
                    id="audit-list",
                )
                yield Static(
                    self.rows[0].details_text() if self.rows else "No audit runs recorded",
                    id="audit-details",
                    markup=False,
                )
            yield Static(
                "/ search   f outcome   b backend   r recency   esc close",
                classes="audit-help",
            )

    def on_mount(self) -> None:
        audit_list = self.query_one("#audit-list", ListView)
        if self.rows:
            audit_list.index = 0
            audit_list.focus()
        self._update_filter_summary(len(self.rows))

    @on(ListView.Highlighted)
    def show_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, AuditListItem):
            self.query_one("#audit-details", Static).update(
                event.item.row.details_text()
            )

    @on(Input.Changed, "#audit-search")
    async def search_changed(self, _event: Input.Changed) -> None:
        await self._refresh()

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_cycle_outcome(self) -> None:
        self._outcome_index = (self._outcome_index + 1) % len(self._outcomes)
        await self._refresh()

    async def action_cycle_backend(self) -> None:
        self._backend_index = (self._backend_index + 1) % len(self._backends)
        await self._refresh()

    async def action_cycle_recency(self) -> None:
        self._recency_index = (self._recency_index + 1) % len(self._recency_days)
        await self._refresh()

    def action_focus_search(self) -> None:
        self.query_one("#audit-search", Input).focus()

    async def _refresh(self) -> None:
        search = self.query_one("#audit-search", Input).value.strip() or None
        days = self._recency_days[self._recency_index]
        selected = filter_rows(
            self.rows,
            AuditFilter(
                outcome=self._outcomes[self._outcome_index],
                backend=self._backends[self._backend_index],
                since_utc=recent_cutoff(days) if days is not None else None,
                search=search,
            ),
        )
        audit_list = self.query_one("#audit-list", ListView)
        await audit_list.clear()
        if selected:
            await audit_list.extend(AuditListItem(row) for row in selected)
            audit_list.index = 0
            self.query_one("#audit-details", Static).update(
                selected[0].details_text()
            )
        else:
            self.query_one("#audit-details", Static).update("No matching audit runs")
        self._update_filter_summary(len(selected))

    def _update_filter_summary(self, count: int) -> None:
        outcome = self._outcomes[self._outcome_index] or "all outcomes"
        backend = self._backends[self._backend_index] or "all backends"
        days = self._recency_days[self._recency_index]
        recency = "all time" if days is None else f"last {days}d"
        self.query_one("#audit-filter-summary", Static).update(
            f"{count} shown · {outcome} · {backend} · {recency}"
        )


def _event_line(event: AuditEvent) -> str:
    timestamp = event.occurred_at.astimezone().strftime("%H:%M:%S")
    parts = [timestamp, event.event_type.value]
    if event.message:
        parts.append(bounded_preview(event.message))
    details = sanitize_detail_rows(event.metadata)
    if details:
        parts.append(" ".join(f"{key}={value}" for key, value in details))
    return " · ".join(parts)


def _clamp(value: str) -> str:
    if len(value) <= MAX_PREVIEW_CHARS:
        return value
    return f"{value[: MAX_PREVIEW_CHARS - 1]}…"
