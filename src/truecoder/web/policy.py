from __future__ import annotations

from dataclasses import dataclass
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import Final
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES: Final[tuple[str, ...]] = ("http", "https")
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
MAX_URL_LENGTH: Final = 2048
MAX_HOST_LENGTH: Final = 253

# is_global already refuses every one of these, and the classification has
# shifted between Python releases. Naming them keeps the boundary stable when
# the standard library moves underneath it.
_REFUSED_NETWORKS: Final[tuple[IPv4Network | IPv6Network, ...]] = tuple(
    ip_network(entry)
    for entry in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class UrlPolicyError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A URL policy error requires a message.")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("A URL policy error requires a code.")

        self.message = message.strip()
        self.code = code.strip()
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class FetchTarget:
    url: str
    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"Unsupported scheme: {self.scheme!r}")
        if not self.host:
            raise ValueError("A fetch target requires a host.")
        if not 1 <= self.port <= 65535:
            raise ValueError("A fetch target requires a valid port.")

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)


def normalize_url(raw: str) -> FetchTarget:
    if not isinstance(raw, str):
        raise UrlPolicyError("The URL must be text.", code="invalid_url")

    candidate = raw.strip()
    if not candidate:
        raise UrlPolicyError("The URL cannot be empty.", code="invalid_url")

    if len(candidate) > MAX_URL_LENGTH:
        raise UrlPolicyError(
            f"The URL exceeds {MAX_URL_LENGTH} characters.",
            code="url_too_long",
        )

    if any(character.isspace() or ord(character) < 0x20 for character in candidate):
        raise UrlPolicyError(
            "The URL contains whitespace or control characters.",
            code="invalid_url",
        )

    try:
        parts = urlsplit(candidate)
    except ValueError as error:
        raise UrlPolicyError(
            "The URL could not be parsed.", code="invalid_url"
        ) from error

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(
            f"Only {' and '.join(ALLOWED_SCHEMES)} URLs can be fetched.",
            code="unsupported_scheme",
        )

    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError(
            "Credentials cannot be supplied in the URL.",
            code="credentials_in_url",
        )

    try:
        hostname = parts.hostname
    except ValueError as error:
        raise UrlPolicyError("The URL host is invalid.", code="invalid_url") from error

    if not hostname:
        raise UrlPolicyError("The URL is missing a host.", code="invalid_url")

    host = hostname.lower().rstrip(".")
    if not host or len(host) > MAX_HOST_LENGTH:
        raise UrlPolicyError("The URL host is invalid.", code="invalid_url")

    try:
        port = parts.port
    except ValueError as error:
        raise UrlPolicyError("The URL port is invalid.", code="invalid_url") from error

    resolved_port = DEFAULT_PORTS[scheme] if port is None else port
    if not 1 <= resolved_port <= 65535:
        raise UrlPolicyError("The URL port is invalid.", code="invalid_url")

    normalized = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return FetchTarget(url=normalized, scheme=scheme, host=host, port=resolved_port)


def _embedded_addresses(
    address: IPv4Address | IPv6Address,
) -> tuple[IPv4Address | IPv6Address, ...]:
    embedded: list[IPv4Address | IPv6Address] = []
    for attribute in ("ipv4_mapped", "sixtofour"):
        candidate = getattr(address, attribute, None)
        if candidate is not None:
            embedded.append(candidate)
    teredo = getattr(address, "teredo", None)
    if teredo is not None:
        embedded.extend(teredo)
    return tuple(embedded)


def address_refusal(address: str | IPv4Address | IPv6Address) -> str | None:
    try:
        parsed = ip_address(address) if isinstance(address, str) else address
    except ValueError:
        return "invalid_address"

    candidates = (parsed, *_embedded_addresses(parsed))
    for candidate in candidates:
        if not candidate.is_global:
            return "address_not_public"
        if any(candidate in network for network in _REFUSED_NETWORKS):
            return "address_not_public"
    return None


def require_public_address(address: str | IPv4Address | IPv6Address) -> None:
    refusal = address_refusal(address)
    if refusal is None:
        return
    if refusal == "invalid_address":
        raise UrlPolicyError("The resolved address is invalid.", code=refusal)
    raise UrlPolicyError(
        "The URL resolves to an address that is not publicly routable.",
        code=refusal,
    )
