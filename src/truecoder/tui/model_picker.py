from __future__ import annotations

from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from truecoder.providers.models import ModelInfo

MAX_VISIBLE_MODELS: Final = 200


class ModelListItem(ListItem):
    def __init__(self, model: ModelInfo, *, active: bool) -> None:
        self.model = model
        self.active = active
        super().__init__(classes="model-item active" if active else "model-item")

    def compose(self) -> ComposeResult:
        marker = "● " if self.active else "  "
        context = self.model.context_label
        suffix = f"   {context}" if context else ""
        yield Static(
            f"{marker}{self.model.identifier}{suffix}",
            classes="model-item-line",
            markup=False,
        )


class ModelPickerScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        models: tuple[ModelInfo, ...],
        active_model: str,
        *,
        unavailable_reason: str | None = None,
    ) -> None:
        self.models = models
        self.active_model = active_model
        self.unavailable_reason = unavailable_reason
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker-dialog"):
            yield Static("Models", classes="model-dialog-title")
            if self.unavailable_reason is not None:
                yield Static(
                    self.unavailable_reason,
                    classes="model-dialog-empty",
                    markup=False,
                )
            else:
                yield Input(placeholder="Filter", id="model-filter")
                yield ListView(*self._items(self.models), id="model-list")
            yield Static(
                "enter select   esc close",
                classes="model-dialog-help",
            )

    def _items(self, models: tuple[ModelInfo, ...]) -> list[ModelListItem]:
        return [
            ModelListItem(model, active=model.identifier == self.active_model)
            for model in models[:MAX_VISIBLE_MODELS]
        ]

    def on_mount(self) -> None:
        if self.unavailable_reason is not None:
            return
        model_list = self.query_one("#model-list", ListView)
        active_index = next(
            (
                index
                for index, model in enumerate(self.models[:MAX_VISIBLE_MODELS])
                if model.identifier == self.active_model
            ),
            0,
        )
        model_list.index = active_index
        self.query_one("#model-filter", Input).focus()

    def visible_models(self, query: str) -> tuple[ModelInfo, ...]:
        return tuple(model for model in self.models if model.matches(query))

    @on(Input.Changed, "#model-filter")
    async def filter_models(self, event: Input.Changed) -> None:
        model_list = self.query_one("#model-list", ListView)
        await model_list.clear()
        matches = self.visible_models(event.value)
        for item in self._items(matches):
            await model_list.append(item)
        model_list.index = 0 if matches else None

    @on(Input.Submitted, "#model-filter")
    def choose_first_match(self, event: Input.Submitted) -> None:
        matches = self.visible_models(event.value)
        if matches:
            self.dismiss(matches[0].identifier)

    @on(ListView.Selected)
    def choose_model(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ModelListItem):
            self.dismiss(event.item.model.identifier)

    def action_cancel(self) -> None:
        self.dismiss(None)
