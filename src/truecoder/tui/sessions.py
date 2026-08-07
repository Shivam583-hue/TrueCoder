from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from truecoder.session import SessionSummary


@dataclass(frozen=True, slots=True)
class SessionAction:
    kind: Literal["new", "switch", "rename", "delete"]
    session_id: str | None = None


class SessionListItem(ListItem):
    def __init__(self, summary: SessionSummary, *, active: bool) -> None:
        self.summary = summary
        self.active = active
        super().__init__()

    def compose(self) -> ComposeResult:
        marker = "●" if self.active else " "
        turns = f"{self.summary.turn_count} turn"
        if self.summary.turn_count != 1:
            turns += "s"
        updated = self.summary.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        yield Label(
            f"{marker}  {self.summary.title}\n   {turns} · {updated}",
            markup=False,
        )


class SessionManagerScreen(ModalScreen[SessionAction | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("n", "new_session", "New", show=False),
        Binding("r", "rename_session", "Rename", show=False),
        Binding("d", "delete_session", "Delete", show=False),
    ]

    def __init__(
        self,
        sessions: tuple[SessionSummary, ...],
        active_session_id: str,
    ) -> None:
        self.sessions = sessions
        self.active_session_id = active_session_id
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="session-manager-dialog"):
            yield Static("Sessions", classes="session-dialog-title")
            yield ListView(
                *(
                    SessionListItem(
                        summary,
                        active=summary.session_id == self.active_session_id,
                    )
                    for summary in self.sessions
                ),
                id="session-list",
            )
            yield Static(
                "enter resume   n new   r rename   d delete   esc close",
                classes="session-dialog-help",
            )

    def on_mount(self) -> None:
        session_list = self.query_one("#session-list", ListView)
        active_index = next(
            (
                index
                for index, summary in enumerate(self.sessions)
                if summary.session_id == self.active_session_id
            ),
            0,
        )
        session_list.index = active_index
        session_list.focus()

    def _selected_session_id(self) -> str | None:
        highlighted = self.query_one("#session-list", ListView).highlighted_child
        if isinstance(highlighted, SessionListItem):
            return highlighted.summary.session_id
        return None

    @on(ListView.Selected)
    def select_session(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionListItem):
            self.dismiss(SessionAction("switch", event.item.summary.session_id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_new_session(self) -> None:
        self.dismiss(SessionAction("new"))

    def action_rename_session(self) -> None:
        session_id = self._selected_session_id()
        if session_id is not None:
            self.dismiss(SessionAction("rename", session_id))

    def action_delete_session(self) -> None:
        session_id = self._selected_session_id()
        if session_id is not None:
            self.dismiss(SessionAction("delete", session_id))


class RenameSessionScreen(ModalScreen[str | None]):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.session_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="session-small-dialog"):
            yield Static("Rename session", classes="session-dialog-title")
            yield Input(
                value=self.session_title,
                id="session-title-input",
                max_length=120,
            )
            yield Static("", id="session-title-error")
            with Horizontal(classes="session-dialog-actions"):
                yield Button("Cancel", id="session-rename-cancel")
                yield Button("Rename", id="session-rename-confirm", variant="primary")

    def on_mount(self) -> None:
        session_input = self.query_one("#session-title-input", Input)
        session_input.focus()
        session_input.select_all()

    @on(Input.Submitted)
    def submit_title(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(Button.Pressed, "#session-rename-confirm")
    def confirm_rename(self) -> None:
        self._submit(self.query_one("#session-title-input", Input).value)

    @on(Button.Pressed, "#session-rename-cancel")
    def cancel_rename(self) -> None:
        self.dismiss(None)

    def _submit(self, title: str) -> None:
        if not title.strip():
            self.query_one("#session-title-error", Static).update(
                "Title cannot be empty."
            )
            return
        self.dismiss(title)


class DeleteSessionScreen(ModalScreen[bool]):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.session_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="session-small-dialog"):
            yield Static("Delete session?", classes="session-dialog-title")
            yield Static(
                f'"{self.session_title}" will be permanently deleted.',
                classes="session-delete-copy",
                markup=False,
            )
            with Horizontal(classes="session-dialog-actions"):
                yield Button("Cancel", id="session-delete-cancel")
                yield Button("Delete", id="session-delete-confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#session-delete-cancel", Button).focus()

    @on(Button.Pressed, "#session-delete-cancel")
    def cancel_delete(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#session-delete-confirm")
    def confirm_delete(self) -> None:
        self.dismiss(True)
