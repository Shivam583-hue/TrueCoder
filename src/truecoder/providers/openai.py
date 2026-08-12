from __future__ import annotations

from typing import Final

from truecoder.providers.models import Provider
from truecoder.providers.oauth import OAuthClient

OPENAI_PROVIDER_NAME: Final = "openai"
OPENAI_DISPLAY_NAME: Final = "OpenAI"
OPENAI_API_BASE_URL: Final = "https://api.openai.com/v1"
OPENAI_CODEX_BASE_URL: Final = "https://chatgpt.com/backend-api/codex"
OPENAI_CODEX_MODELS_URL: Final = (
    "https://chatgpt.com/backend-api/codex/models?client_version=0.1.0"
)
OPENAI_CODEX_CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_AUTHORIZE_URL: Final = "https://auth.openai.com/oauth/authorize"
OPENAI_TOKEN_URL: Final = "https://auth.openai.com/oauth/token"
OPENAI_DEVICE_URL: Final = "https://auth.openai.com/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_TOKEN_URL: Final = (
    "https://auth.openai.com/api/accounts/deviceauth/token"
)
OPENAI_DEVICE_VERIFICATION_URL: Final = "https://auth.openai.com/codex/device"
OPENAI_DEVICE_REDIRECT_URL: Final = "https://auth.openai.com/deviceauth/callback"
OPENAI_REDIRECT_PORT: Final = 1455
OPENAI_REDIRECT_HOST: Final = "localhost"
OPENAI_REDIRECT_PATH: Final = "/auth/callback"
OPENAI_ACCOUNT_CLAIM: Final = "chatgpt_account_id"
OPENAI_ACCOUNT_HEADER: Final = "ChatGPT-Account-Id"

OPENAI_OAUTH_CLIENT: Final = OAuthClient(
    client_id=OPENAI_CODEX_CLIENT_ID,
    authorize_url=OPENAI_AUTHORIZE_URL,
    token_url=OPENAI_TOKEN_URL,
    scopes=("openid", "profile", "email", "offline_access"),
    account_claim=OPENAI_ACCOUNT_CLAIM,
    account_header=OPENAI_ACCOUNT_HEADER,
    api_base_url=OPENAI_CODEX_BASE_URL,
    models_url=OPENAI_CODEX_MODELS_URL,
    extra_parameters=(
        ("id_token_add_organizations", "true"),
        ("codex_cli_simplified_flow", "true"),
        ("originator", "truecoder"),
    ),
    redirect_port=OPENAI_REDIRECT_PORT,
    redirect_host=OPENAI_REDIRECT_HOST,
    redirect_path=OPENAI_REDIRECT_PATH,
    device_url=OPENAI_DEVICE_URL,
    device_token_url=OPENAI_DEVICE_TOKEN_URL,
    device_verification_url=OPENAI_DEVICE_VERIFICATION_URL,
    device_redirect_url=OPENAI_DEVICE_REDIRECT_URL,
)


def openai_provider(*, name: str = OPENAI_PROVIDER_NAME) -> Provider:
    return Provider(
        name=name,
        display_name=OPENAI_DISPLAY_NAME,
        base_url=OPENAI_API_BASE_URL,
        oauth=OPENAI_OAUTH_CLIENT,
        header_pairs=(("originator", "truecoder"), ("version", "0.1.0")),
        wire_api="responses",
        adapter="openai",
        env_names=("OPENAI_API_KEY",),
    )


def is_openai_provider(provider: Provider) -> bool:
    return provider.oauth == OPENAI_OAUTH_CLIENT
