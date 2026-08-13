from __future__ import annotations

from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

EFFORT_DESCRIPTIONS: Final = {
    "none": "No deliberate reasoning",
    "minimal": "Very light reasoning",
    "low": "Faster, lighter reasoning",
    "medium": "Balanced speed and depth",
    "high": "Deeper reasoning",
    "xhigh": "Very deep reasoning",
    "max": "Maximum available reasoning",
}


class ReasoningEffortItem(ListItem):
    def __init__(self, effort: str, *, active: bool) -> None:
        self.effort = effort
        self.active = active
        classes = "effort-item active" if active else "effort-item"
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        marker = "● " if self.active else "  "
        description = EFFORT_DESCRIPTIONS.get(self.effort, "Reasoning effort")
        yield Static(
            f"{marker}{self.effort.ljust(9)}  {description}",
            classes="effort-item-line",
            markup=False,
        )


class ReasoningEffortScreen(ModalScreen["str | None"]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, efforts: tuple[str, ...], active: str) -> None:
        self.efforts = efforts
        self.active = active
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="effort-picker-dialog"):
            yield Static(
                "Reasoning effort",
                classes="model-dialog-title",
                markup=False,
            )
            yield Static(
                "Higher effort can improve difficult work, but takes longer.",
                classes="effort-dialog-note",
                markup=False,
            )
            yield ListView(
                *(
                    ReasoningEffortItem(effort, active=effort == self.active)
                    for effort in self.efforts
                ),
                id="effort-list",
            )
            yield Static(
                "enter select   esc close",
                classes="model-dialog-help",
                markup=False,
            )

    def on_mount(self) -> None:
        selected = next(
            (
                index
                for index, effort in enumerate(self.efforts)
                if effort == self.active
            ),
            0,
        )
        for effort_list in self.query("#effort-list"):
            if isinstance(effort_list, ListView):
                effort_list.index = selected
                effort_list.focus()

    @on(ListView.Selected, "#effort-list")
    def choose_effort(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ReasoningEffortItem):
            self.dismiss(event.item.effort)

    def action_cancel(self) -> None:
        self.dismiss(None)
