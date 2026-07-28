from pathlib import Path

_SENSITIVE_DIRECTORY_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".credentials",
        ".netrc",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_SENSITIVE_FILE_SUFFIXES = frozenset({".jks", ".key", ".p12", ".pem", ".pfx"})
_SAFE_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_sensitive_path(workspace_path: Path) -> bool:
    normalized_parts = tuple(part.casefold() for part in workspace_path.parts)
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in normalized_parts):
        return True

    file_name = workspace_path.name.casefold()
    if file_name in _SAFE_ENV_TEMPLATE_NAMES:
        return False

    if file_name == ".env" or file_name.startswith(".env."):
        return True

    if file_name in _SENSITIVE_FILE_NAMES:
        return True

    return workspace_path.suffix.casefold() in _SENSITIVE_FILE_SUFFIXES
