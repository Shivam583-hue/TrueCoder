from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from truecoder.providers.models import ModelInfo, Provider
from truecoder.providers.openai import is_openai_provider

MAX_VISIBLE_MODELS: Final = 200
IDENTIFIER_COLUMN: Final = 38
PROVIDER_COLUMN: Final = 14
CONTEXT_COLUMN: Final = 6
GAP: Final = "  "
PROVIDER_PRIORITY: Final = {
    "openai": 0,
    "anthropic": 1,
    "google": 2,
    "openrouter": 3,
}


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    provider: Provider
    connected: bool = False
    model_count: int = 0

    @property
    def searchable(self) -> str:
        return (
            f"{self.provider.name} {self.provider.label} "
            f"{self.connection_hint}"
        )

    @property
    def connection_hint(self) -> str:
        if is_openai_provider(self.provider):
            return "ChatGPT Plus/Pro or API key"
        if self.provider.oauth is not None:
            return "Browser sign-in or API key"
        return "API key"

    def matches(self, query: str) -> bool:
        needle = query.strip().casefold()
        return all(part in self.searchable.casefold() for part in needle.split())


class ProviderListItem(ListItem):
    def __init__(self, choice: ProviderChoice) -> None:
        self.choice = choice
        super().__init__(classes="model-item")

    def line(self) -> str:
        marker = "✓ " if self.choice.connected else "  "
        suffix = self.choice.connection_hint
        if self.choice.connected:
            if self.choice.model_count:
                noun = "model" if self.choice.model_count == 1 else "models"
                suffix = f"{self.choice.model_count} {noun}"
            else:
                suffix = "connected"
        return f"{marker}{self.choice.provider.label}  {suffix}"

    def compose(self) -> ComposeResult:
        yield Static(self.line(), classes="model-item-line", markup=False)


class ProviderPickerScreen(ModalScreen["ProviderChoice | None"]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        providers: tuple[ProviderChoice, ...],
        *,
        dialog_title: str = "All providers",
        cancel_label: str = "close",
    ) -> None:
        self.providers = tuple(
            sorted(
                providers,
                key=lambda choice: (
                    PROVIDER_PRIORITY.get(choice.provider.name, 99),
                    choice.provider.label.casefold(),
                ),
            )
        )
        self.dialog_title = dialog_title
        self.cancel_label = cancel_label
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-picker-dialog"):
            yield Static(
                self.dialog_title,
                classes="model-dialog-title",
                markup=False,
            )
            yield Input(placeholder="Search providers", id="provider-filter")
            yield ListView(*self._items(self.providers), id="provider-list")
            yield Static(
                f"enter select   esc {self.cancel_label}",
                classes="model-dialog-help",
                markup=False,
            )

    @staticmethod
    def _items(providers: tuple[ProviderChoice, ...]) -> list[ProviderListItem]:
        return [ProviderListItem(choice) for choice in providers]

    def visible_providers(self, query: str) -> tuple[ProviderChoice, ...]:
        return tuple(choice for choice in self.providers if choice.matches(query))

    def on_mount(self) -> None:
        for provider_list in self.query("#provider-list"):
            if isinstance(provider_list, ListView):
                provider_list.index = 0 if self.providers else None
        self.query("#provider-filter").focus()

    @on(Input.Changed, "#provider-filter")
    async def filter_providers(self, event: Input.Changed) -> None:
        provider_list = self.query_one("#provider-list", ListView)
        await provider_list.clear()
        rows = self._items(self.visible_providers(event.value))
        for item in rows:
            await provider_list.append(item)
        provider_list.index = 0 if rows else None

    @on(Input.Submitted, "#provider-filter")
    def choose_first_match(self, event: Input.Submitted) -> None:
        matches = self.visible_providers(event.value)
        if matches:
            self.dismiss(matches[0])

    @on(ListView.Selected, "#provider-list")
    def choose_provider(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ProviderListItem):
            self.dismiss(event.item.choice)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class ProviderInvite:
    provider: str
    oauth: bool = False
    display_name: str = ""

    @property
    def label(self) -> str:
        verb = "Connect to" if self.oauth else "Add a key for"
        return f"  {verb} {self.display_name or self.provider} to list its models"

    def matches(self, query: str) -> bool:
        needle = query.strip().casefold()
        target = f"{self.provider} {self.display_name}".casefold()
        return all(part in target for part in needle.split())


class InviteListItem(ListItem):
    def __init__(self, invitation: ProviderInvite) -> None:
        self.invitation = invitation
        super().__init__(classes="model-item invite")

    def compose(self) -> ComposeResult:
        yield Static(
            self.invitation.label,
            classes="model-item-line invite-line",
            markup=False,
        )


class ModelListItem(ListItem):
    def __init__(
        self,
        model: ModelInfo,
        *,
        active: bool,
        identifier_width: int = 0,
        provider_width: int = 0,
        source: str = "",
    ) -> None:
        self.model = model
        self.active = active
        self.identifier_width = identifier_width
        self.provider_width = provider_width
        self.source = source or model.provider
        super().__init__(classes="model-item active" if active else "model-item")

    def line(self) -> str:
        marker = "● " if self.active else "  "
        row = marker + self.model.label.ljust(self.identifier_width)
        if self.provider_width:
            row += GAP + self.source.ljust(self.provider_width)
        return (row + GAP + self.model.context_label.rjust(CONTEXT_COLUMN)).rstrip()

    def compose(self) -> ComposeResult:
        yield Static(self.line(), classes="model-item-line", markup=False)


class SectionListItem(ListItem):
    def __init__(self, label: str) -> None:
        self.label = label
        super().__init__(classes="model-section", disabled=True)

    def compose(self) -> ComposeResult:
        yield Static(self.label, classes="model-section-line", markup=False)


class ModelPickerAction(Enum):
    ALL_PROVIDERS = auto()


class ModelPickerScreen(
    ModalScreen[
        "ModelInfo | ProviderChoice | ProviderInvite | ModelPickerAction | None"
    ]
):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+a", "all_providers", "All providers", show=False),
    ]

    def __init__(
        self,
        models: tuple[ModelInfo, ...],
        active_model: str,
        *,
        active_provider: str = "",
        invitations: tuple[ProviderInvite, ...] = (),
        providers: tuple[ProviderChoice, ...] = (),
        sources: dict[str, str] | None = None,
        unavailable_reason: str | None = None,
        dialog_title: str = "Models",
        allow_provider_browser: bool = False,
        cancel_label: str = "close",
    ) -> None:
        self.active_model = active_model
        self.active_provider = active_provider
        self.invitations = invitations
        self.providers = tuple(
            sorted(
                providers,
                key=lambda choice: (
                    PROVIDER_PRIORITY.get(choice.provider.name, 99),
                    choice.provider.label.casefold(),
                ),
            )
        )
        self.sources = sources or {}
        self.models = self._ordered_models(models)
        self.unavailable_reason = unavailable_reason
        self.dialog_title = dialog_title
        self.allow_provider_browser = allow_provider_browser
        self.cancel_label = cancel_label
        super().__init__()

    def source_of(self, provider: str) -> str:
        return self.sources.get(provider, provider)

    def served_by(self) -> str:
        names = {model.provider for model in self.models}
        if len(names) != 1:
            return ""
        only = names.pop()
        label = self.sources.get(only, "")
        return label if label and label != only else ""

    def _provider_rank(self, provider: str) -> tuple[int, int, str]:
        if provider == self.active_provider:
            return 0, 0, ""
        priority = PROVIDER_PRIORITY.get(provider)
        if priority is not None:
            return 1, priority, ""
        return 2, 0, self.source_of(provider).casefold()

    def _ordered_models(
        self,
        models: tuple[ModelInfo, ...],
    ) -> tuple[ModelInfo, ...]:
        ordered = sorted(
            models,
            key=lambda model: (model.label.casefold(), model.identifier.casefold()),
        )
        ordered.sort(key=lambda model: model.release_date, reverse=True)
        ordered.sort(key=lambda model: self._provider_rank(model.provider))
        ordered.sort(key=lambda model: 0 if self.is_active(model) else 1)
        return tuple(ordered)

    @property
    def spans_providers(self) -> bool:
        return len({model.provider for model in self.models}) > 1

    def is_active(self, model: ModelInfo) -> bool:
        if model.identifier != self.active_model:
            return False
        if not self.active_provider:
            return True
        return model.provider == self.active_provider

    @property
    def has_list(self) -> bool:
        return bool(self.models or self.invitations or self.providers)

    def compose(self) -> ComposeResult:
        served = self.served_by()
        with Vertical(id="model-picker-dialog"):
            with Horizontal(classes="model-dialog-heading"):
                yield Static(
                    self.dialog_title,
                    classes="model-dialog-title",
                    markup=False,
                )
                if self.allow_provider_browser:
                    yield Static(
                        "ctrl+a  all providers",
                        classes="model-dialog-shortcut",
                        markup=False,
                    )
            if served:
                yield Static(
                    f"Served by {served}",
                    classes="model-dialog-source",
                    markup=False,
                )
            if self.unavailable_reason is not None:
                yield Static(
                    self.unavailable_reason,
                    classes="model-dialog-empty",
                    markup=False,
                )
            if self.has_list:
                yield Input(placeholder="Filter", id="model-filter")
                yield ListView(
                    *self._items(
                        self.models,
                        self.invitations,
                        self.providers,
                    ),
                    id="model-list",
                )
            yield Static(
                (
                    f"ctrl+a all providers   enter select   esc {self.cancel_label}"
                    if self.allow_provider_browser
                    else f"enter select   esc {self.cancel_label}"
                ),
                classes="model-dialog-help",
                markup=False,
            )

    def columns(self) -> tuple[int, int]:
        identifiers = min(
            max((len(model.label) for model in self.models), default=0),
            IDENTIFIER_COLUMN,
        )
        if not self.spans_providers:
            return identifiers, 0
        providers = min(
            max(
                (len(self.source_of(model.provider)) for model in self.models),
                default=0,
            ),
            PROVIDER_COLUMN,
        )
        return identifiers, providers

    def _items(
        self,
        models: tuple[ModelInfo, ...],
        invitations: tuple[ProviderInvite, ...] = (),
        provider_choices: tuple[ProviderChoice, ...] = (),
    ) -> list[ListItem]:
        identifiers, provider_width = self.columns()
        rows: list[ListItem] = []
        has_provider_sections = bool(invitations or provider_choices)
        if provider_choices:
            rows.append(SectionListItem("Popular providers"))
            rows.extend(ProviderListItem(choice) for choice in provider_choices)
        if models and has_provider_sections:
            rows.append(SectionListItem("Models"))
        rows.extend(
            ModelListItem(
                model,
                active=self.is_active(model),
                identifier_width=identifiers,
                provider_width=provider_width,
                source=self.source_of(model.provider),
            )
            for model in models[:MAX_VISIBLE_MODELS]
        )
        if invitations:
            rows.append(SectionListItem("More providers"))
            rows.extend(InviteListItem(invitation) for invitation in invitations)
        return rows

    @staticmethod
    def _selected_index(rows: list[ListItem]) -> int | None:
        provider = next(
            (
                index
                for index, row in enumerate(rows)
                if isinstance(row, ProviderListItem)
            ),
            None,
        )
        if provider is not None:
            return provider
        active = next(
            (
                index
                for index, row in enumerate(rows)
                if isinstance(row, ModelListItem) and row.active
            ),
            None,
        )
        if active is not None:
            return active
        return next(
            (
                index
                for index, row in enumerate(rows)
                if not isinstance(row, SectionListItem)
            ),
            None,
        )

    def on_mount(self) -> None:
        if not self.has_list:
            return
        for model_list in self.query("#model-list"):
            if isinstance(model_list, ListView):
                model_list.index = self._selected_index(
                    self._items(self.models, self.invitations, self.providers)
                )
        self.query("#model-filter").focus()

    def visible_models(self, query: str) -> tuple[ModelInfo, ...]:
        needle = query.strip().casefold()
        return tuple(
            model
            for model in self.models
            if model.matches(query)
            or all(
                part in self.source_of(model.provider).casefold()
                for part in needle.split()
            )
        )

    def visible_invitations(self, query: str) -> tuple[ProviderInvite, ...]:
        return tuple(
            invitation
            for invitation in self.invitations
            if invitation.matches(query)
        )

    def visible_providers(self, query: str) -> tuple[ProviderChoice, ...]:
        return tuple(choice for choice in self.providers if choice.matches(query))

    @on(Input.Changed, "#model-filter")
    async def filter_models(self, event: Input.Changed) -> None:
        model_list = self.query_one("#model-list", ListView)
        await model_list.clear()
        matches = self.visible_models(event.value)
        rows = self._items(
            matches,
            self.visible_invitations(event.value),
            self.visible_providers(event.value),
        )
        for item in rows:
            await model_list.append(item)
        model_list.index = self._selected_index(rows)

    @on(Input.Submitted, "#model-filter")
    def choose_first_match(self, event: Input.Submitted) -> None:
        providers = self.visible_providers(event.value)
        if providers:
            self.dismiss(providers[0])
            return
        matches = self.visible_models(event.value)
        if matches:
            self.dismiss(matches[0])
            return
        invitations = self.visible_invitations(event.value)
        if invitations:
            self.dismiss(invitations[0])

    @on(ListView.Selected)
    def choose_model(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ModelListItem):
            self.dismiss(event.item.model)
        elif isinstance(event.item, InviteListItem):
            self.dismiss(event.item.invitation)
        elif isinstance(event.item, ProviderListItem):
            self.dismiss(event.item.choice)

    def action_all_providers(self) -> None:
        if self.allow_provider_browser:
            self.dismiss(ModelPickerAction.ALL_PROVIDERS)

    def action_cancel(self) -> None:
        self.dismiss(None)
