from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from truecoder.providers.models import DEFAULT_PROVIDER_NAME

MAX_DETAIL_CHARS: Final = 400
TRUNCATION: Final = "...[truncated]"
UNNAMED_PROVIDER: Final = "The provider"

CREDENTIAL: Final = "credential"
BILLING: Final = "billing"
RATE_LIMIT: Final = "rate-limit"
UNKNOWN_MODEL: Final = "unknown-model"
TIMEOUT: Final = "timeout"
NETWORK: Final = "network"
PROVIDER: Final = "provider"

PARTIAL_NOTICE: Final = "The reply above stopped where it did because of this."

_MESSAGE_KEYS: Final = ("message", "detail", "description")


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    kind: str
    summary: str
    detail: str = ""
    status: int | None = None

    @property
    def message(self) -> str:
        if not self.detail:
            return self.summary
        return f"{self.summary}\n\nThe provider said: {self.detail}"


def bounded(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_DETAIL_CHARS:
        return collapsed
    return collapsed[:MAX_DETAIL_CHARS] + TRUNCATION


def provider_message(body: Any) -> str:
    if isinstance(body, str):
        return bounded(body)
    if not isinstance(body, dict):
        return ""

    error = body.get("error")
    if isinstance(error, str) and error.strip():
        return bounded(error)
    if isinstance(error, dict):
        for key in _MESSAGE_KEYS:
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return bounded(value)
    for key in _MESSAGE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return bounded(value)
    return ""


def named(provider: str) -> str:
    if not isinstance(provider, str) or not provider.strip():
        return UNNAMED_PROVIDER
    return UNNAMED_PROVIDER if provider == DEFAULT_PROVIDER_NAME else provider


def classify(
    *,
    status: int | None,
    body: Any = None,
    fallback: str = "",
    provider: str = "",
    model: str = "",
    partial: bool = False,
) -> ProviderFailure:
    who = named(provider)
    detail = provider_message(body) or bounded(fallback)

    if status in (401, 403):
        kind, summary = CREDENTIAL, f"{who} rejected the credential in use."
    elif status == 402:
        kind, summary = BILLING, f"{who} refused the request over billing."
    elif status == 429:
        kind, summary = RATE_LIMIT, f"{who} is rate limiting this credential."
    elif status == 404:
        subject = f"the model {model}" if model else "that model"
        kind, summary = UNKNOWN_MODEL, f"{who} does not recognise {subject}."
    elif status is not None and status >= 500:
        kind, summary = PROVIDER, f"{who} failed to answer ({status})."
    elif status is not None:
        kind, summary = PROVIDER, f"{who} refused the request ({status})."
    else:
        kind, summary = PROVIDER, f"{who} did not complete the request."

    if partial:
        summary = f"{summary} {PARTIAL_NOTICE}"
    return ProviderFailure(kind=kind, summary=summary, detail=detail, status=status)


def classify_exception(
    error: object,
    *,
    provider: str = "",
    model: str = "",
    partial: bool = False,
) -> ProviderFailure:
    status = getattr(error, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    return classify(
        status=status,
        body=getattr(error, "body", None),
        fallback=str(error),
        provider=provider,
        model=model,
        partial=partial,
    )


def timed_out(provider: str, *, partial: bool = False) -> ProviderFailure:
    summary = f"{named(provider)} did not answer in time."
    if partial:
        summary = f"{summary} {PARTIAL_NOTICE}"
    return ProviderFailure(kind=TIMEOUT, summary=summary)


def unreachable(provider: str, detail: str = "", *, partial: bool = False) -> ProviderFailure:
    summary = f"{named(provider)} could not be reached."
    if partial:
        summary = f"{summary} {PARTIAL_NOTICE}"
    return ProviderFailure(kind=NETWORK, summary=summary, detail=bounded(detail))


def remedy(kind: str, *, oauth: bool = False) -> str:
    if kind == CREDENTIAL:
        if oauth:
            return "Choose how to connect again in the prompt, or run /login later."
        return "Type a new key in the prompt, or run /login to enter one later."
    if kind == BILLING:
        return "Add credit with the provider, or pick a cheaper model with /models."
    if kind == RATE_LIMIT:
        return "Wait a moment and send it again, or pick another model with /models."
    if kind == UNKNOWN_MODEL:
        return "Pick another model with /models."
    if kind in (TIMEOUT, NETWORK):
        return "Check your connection and send it again."
    return ""
