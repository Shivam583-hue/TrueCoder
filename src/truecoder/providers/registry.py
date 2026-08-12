from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

from truecoder.providers.models import Provider

OPENROUTER_PROVIDER_NAME: Final = "openrouter"
OPENROUTER_DISPLAY_NAME: Final = "OpenRouter"
OPENROUTER_API_BASE_URL: Final = "https://openrouter.ai/api/v1"
CUSTOM_PROVIDER_NAME: Final = "custom"
CUSTOM_PROVIDER_DISPLAY_NAME: Final = "Custom provider"


def openrouter_provider(*, base_url: str = OPENROUTER_API_BASE_URL) -> Provider:
    return Provider(
        name=OPENROUTER_PROVIDER_NAME,
        display_name=OPENROUTER_DISPLAY_NAME,
        base_url=base_url,
        header_pairs=(("X-Title", "TrueCoder"),),
        adapter="openai-compatible",
        env_names=("OPENROUTER_API_KEY",),
    )


def provider_from_url(base_url: str) -> Provider:
    host = (urlparse(base_url).hostname or "").casefold()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return openrouter_provider(base_url=base_url)
    return Provider(
        name=CUSTOM_PROVIDER_NAME,
        display_name=CUSTOM_PROVIDER_DISPLAY_NAME,
        base_url=base_url,
    )
