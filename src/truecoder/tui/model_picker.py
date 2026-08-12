from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from truecoder.providers.models import ModelInfo, Provider

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
        return f"{self.provider.name} {self.provider.label}"

    def matches(self, query: str) -> bool:
        needle = query.strip().casefold()
        return all(part in self.searchable.casefold() for part in needle.split())


class ProviderListItem(ListItem):
    def __init__(self, choice: ProviderChoice) -> None:
        self.choice = choice
        super().__init__(classes="model-item")

    def line(self) -> str:
        marker = "✓ " if self.choice.connected else "  "
        suffix = "connected" if self.choice.connected else "API key"
        if self.choice.provider.oauth is not None:
            suffix = "ChatGPT or API key"
        return f"{marker}{self.choice.provider.label}  {suffix}"

    def compose(self) -> ComposeResult:
        yield Static(self.line(), classes="model-item-line", markup=False)


class ProviderPickerScreen(ModalScreen["ProviderChoice | None"]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, providers: tuple[ProviderChoice, ...]) -> None:
        self.providers = tuple(
            sorted(
                providers,
                key=lambda choice: (
                    PROVIDER_PRIORITY.get(choice.provider.name, 99),
                    choice.provider.label.casefold(),
                ),
            )
        )
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-picker-dialog"):
            yield Static("Connect a provider", classes="model-dialog-title")
            yield Input(placeholder="Search providers", id="provider-filter")
            yield ListView(*self._items(self.providers), id="provider-list")
            yield Static("enter select   esc close", classes="model-dialog-help")

    @staticmethod
    def _items(providers: tuple[ProviderChoice, ...]) -> list[ProviderListItem]:
        return [ProviderListItem(choice) for choice in providers]

    def visible_providers(self, query: str) -> tuple[ProviderChoice, ...]:
        return tuple(choice for choice in self.providers if choice.matches(query))

    def on_mount(self) -> None:
        provider_list = self.query_one("#provider-list", ListView)
        provider_list.index = 0 if self.providers else None
        self.query_one("#provider-filter", Input).focus()

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


class ModelPickerScreen(ModalScreen["ModelInfo | ProviderInvite | None"]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        models: tuple[ModelInfo, ...],
        active_model: str,
        *,
        active_provider: str = "",
        invitations: tuple[ProviderInvite, ...] = (),
        sources: dict[str, str] | None = None,
        unavailable_reason: str | None = None,
        dialog_title: str = "Models",
    ) -> None:
        self.active_model = active_model
        self.active_provider = active_provider
        self.invitations = invitations
        self.sources = sources or {}
        self.models = self._ordered_models(models)
        self.unavailable_reason = unavailable_reason
        self.dialog_title = dialog_title
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
        return bool(self.models or self.invitations)

    def compose(self) -> ComposeResult:
        served = self.served_by()
        with Vertical(id="model-picker-dialog"):
            yield Static(
                self.dialog_title,
                classes="model-dialog-title",
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
                    *self._items(self.models, self.invitations),
                    id="model-list",
                )
            yield Static(
                "enter select   esc close",
                classes="model-dialog-help",
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
    ) -> list[ListItem]:
        identifiers, providers = self.columns()
        rows: list[ListItem] = [
            ModelListItem(
                model,
                active=self.is_active(model),
                identifier_width=identifiers,
                provider_width=providers,
                source=self.source_of(model.provider),
            )
            for model in models[:MAX_VISIBLE_MODELS]
        ]
        rows.extend(InviteListItem(invitation) for invitation in invitations)
        return rows

    def on_mount(self) -> None:
        if not self.has_list:
            return
        model_list = self.query_one("#model-list", ListView)
        active_index = next(
            (
                index
                for index, model in enumerate(self.models[:MAX_VISIBLE_MODELS])
                if self.is_active(model)
            ),
            0,
        )
        model_list.index = active_index
        self.query_one("#model-filter", Input).focus()

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

    @on(Input.Changed, "#model-filter")
    async def filter_models(self, event: Input.Changed) -> None:
        model_list = self.query_one("#model-list", ListView)
        await model_list.clear()
        matches = self.visible_models(event.value)
        rows = self._items(matches, self.visible_invitations(event.value))
        for item in rows:
            await model_list.append(item)
        model_list.index = 0 if rows else None

    @on(Input.Submitted, "#model-filter")
    def choose_first_match(self, event: Input.Submitted) -> None:
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

    def action_cancel(self) -> None:
        self.dismiss(None)
