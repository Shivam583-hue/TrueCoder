from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Final

from truecoder.providers.models import Provider
from truecoder.providers.oauth import OAuthClient

OPENAI_PROVIDER_NAME: Final = "openai"
OPENAI_DISPLAY_NAME: Final = "OpenAI"
OPENAI_API_BASE_URL: Final = "https://api.openai.com/v1"
OPENAI_CODEX_BASE_URL: Final = "https://chatgpt.com/backend-api/codex"
# This identifies the upstream Codex request contract TrueCoder implements. It is
# deliberately independent from TrueCoder's own package version: the ChatGPT
# backend uses it to decide which models are compatible with the client.
OPENAI_CODEX_PROTOCOL_VERSION: Final = "0.144.0"
OPENAI_CODEX_MODELS_URL: Final = (
    "https://chatgpt.com/backend-api/codex/models"
    f"?client_version={OPENAI_CODEX_PROTOCOL_VERSION}"
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


def _user_agent() -> str:
    try:
        package_version = version("truecoder")
    except PackageNotFoundError:
        package_version = "dev"
    return f"truecoder/{package_version}"


OPENAI_CODEX_USER_AGENT: Final = _user_agent()

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
        header_pairs=(
            ("originator", "truecoder"),
            ("User-Agent", OPENAI_CODEX_USER_AGENT),
            ("version", OPENAI_CODEX_PROTOCOL_VERSION),
        ),
        wire_api="responses",
        adapter="openai",
        env_names=("OPENAI_API_KEY",),
    )


def is_openai_provider(provider: Provider) -> bool:
    return provider.oauth == OPENAI_OAUTH_CLIENT
