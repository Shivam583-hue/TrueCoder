from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

from truecoder.memory import MemoryEntry


@dataclass(frozen=True, slots=True)
class MemoryAction:
    kind: Literal["forget", "clear"]
    entry_id: str | None = None


def describe_moment(entry: MemoryEntry) -> str:
    moment = entry.moment
    if moment is None:
        return "unknown time"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class MemoryListItem(ListItem):
    def __init__(self, entry: MemoryEntry) -> None:
        self.entry = entry
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(
            f"{self.entry.note}\n   {describe_moment(self.entry)}",
            markup=False,
        )


class MemoryBrowserScreen(ModalScreen[MemoryAction | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("d", "forget_entry", "Forget", show=False),
        Binding("c", "clear_all", "Clear", show=False),
    ]

    def __init__(self, entries: tuple[MemoryEntry, ...]) -> None:
        self.entries = entries
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-dialog"):
            yield Static("Memory", classes="session-dialog-title")

            if not self.entries:
                yield Static(
                    "Nothing recorded for this workspace yet.",
                    id="memory-empty",
                    markup=False,
                )
                return

            yield Static(
                f"{len(self.entries)} note(s) sent to the model before every reply",
                id="memory-summary",
                markup=False,
            )
            yield ListView(
                *(MemoryListItem(entry) for entry in self.entries),
                id="memory-list",
            )
            yield Static(
                "d forgets the selected note    c clears every note    escape closes",
                classes="session-dialog-help",
                markup=False,
            )

    def on_mount(self) -> None:
        if self.entries:
            self.query_one("#memory-list", ListView).focus()

    def _selected(self) -> MemoryEntry | None:
        if not self.entries:
            return None
        listing = self.query_one("#memory-list", ListView)
        item = listing.highlighted_child
        return item.entry if isinstance(item, MemoryListItem) else None

    @on(ListView.Selected, "#memory-list")
    def forget_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MemoryListItem):
            self.dismiss(MemoryAction(kind="forget", entry_id=item.entry.entry_id))

    def action_forget_entry(self) -> None:
        entry = self._selected()
        if entry is not None:
            self.dismiss(MemoryAction(kind="forget", entry_id=entry.entry_id))

    def action_clear_all(self) -> None:
        if self.entries:
            self.dismiss(MemoryAction(kind="clear"))

    def action_cancel(self) -> None:
        self.dismiss(None)
