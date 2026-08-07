from __future__ import annotations

import asyncio
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urljoin

import httpx

from truecoder.web.extract import (
    MAX_EXTRACTED_CHARACTERS,
    ExtractedDocument,
    extract_plain_text,
    extract_text,
)
from truecoder.web.policy import (
    FetchTarget,
    UrlPolicyError,
    normalize_url,
    require_public_address,
)

DEFAULT_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_REDIRECTS: Final = 5
USER_AGENT: Final = "TrueCoder/0.1 (+https://github.com/Shivam583-hue/TrueCoder)"

HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES: Final = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/xml",
        "application/json",
        "application/xml",
        "application/yaml",
        "text/x-rst",
    }
)


class Resolver(Protocol):
    def __call__(self, host: str, port: int) -> Sequence[str]: ...


def system_resolver(host: str, port: int) -> Sequence[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise UrlPolicyError(
            "The host name could not be resolved.",
            code="dns_failure",
        ) from error

    addresses: list[str] = []
    for record in records:
        address = str(record[4][0])
        if address not in addresses:
            addresses.append(address)

    if not addresses:
        raise UrlPolicyError(
            "The host name did not resolve to any address.",
            code="dns_failure",
        )
    return tuple(addresses)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str
    title: str
    text: str
    truncated: bool
    redirects: tuple[str, ...]


class WebFetchError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _charset(content_type: str) -> str:
    for parameter in content_type.split(";")[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().lower() == "charset":
            return value.strip().strip('"') or "utf-8"
    return "utf-8"


def _pin(target: FetchTarget, address: str) -> httpx.URL:
    host = f"[{address}]" if ":" in address else address
    return httpx.URL(target.url).copy_with(host=host, port=target.port)


class WebFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        resolver: Resolver | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_characters: int = MAX_EXTRACTED_CHARACTERS,
        max_redirects: int = MAX_REDIRECTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._client = client
        self._owns_client = client is None
        self._resolver = resolver or system_resolver
        self._max_response_bytes = max_response_bytes
        self._max_characters = max_characters
        self._max_redirects = max_redirects
        self._timeout_seconds = timeout_seconds

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=self._timeout_seconds,
                verify=True,
            )
        return self._client

    async def fetch(self, raw_url: str) -> FetchedPage:
        target = normalize_url(raw_url)
        requested = target.url
        redirects: list[str] = []

        for _ in range(self._max_redirects + 1):
            response = await self._request(target)
            location = response.headers.get("location")

            if response.is_redirect and location:
                await response.aclose()
                target = self._next_hop(target, location)
                redirects.append(target.url)
                continue

            return await self._read(response, target, requested, tuple(redirects))

        raise WebFetchError(
            f"The URL redirected more than {self._max_redirects} times.",
            code="too_many_redirects",
        )

    def _next_hop(self, target: FetchTarget, location: str) -> FetchTarget:
        try:
            return normalize_url(urljoin(target.url, location))
        except UrlPolicyError as error:
            raise WebFetchError(
                f"The redirect target was refused: {error.message}",
                code=error.code,
            ) from error

    async def _request(self, target: FetchTarget) -> httpx.Response:
        try:
            addresses = await asyncio.to_thread(
                self._resolver,
                target.host,
                target.port,
            )
        except OSError as error:
            raise UrlPolicyError(
                "The host name could not be resolved.",
                code="dns_failure",
            ) from error

        if not addresses:
            raise UrlPolicyError(
                "The host name did not resolve to any address.",
                code="dns_failure",
            )

        for address in addresses:
            require_public_address(address)

        client = self._ensure_client()
        request = client.build_request(
            "GET",
            _pin(target, addresses[0]),
            headers={
                "Host": target.host,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": target.host},
        )
        return await client.send(request, stream=True)

    async def _read(
        self,
        response: httpx.Response,
        target: FetchTarget,
        requested: str,
        redirects: tuple[str, ...],
    ) -> FetchedPage:
        try:
            if response.status_code >= 400:
                raise WebFetchError(
                    f"The server returned HTTP {response.status_code}.",
                    code="http_error",
                )

            content_type = response.headers.get("content-type", "")
            media_type = _media_type(content_type)
            if media_type not in HTML_TYPES and media_type not in TEXT_TYPES:
                raise WebFetchError(
                    f"Unsupported content type: {media_type or 'unknown'}.",
                    code="unsupported_content_type",
                )

            body = bytearray()
            overflowed = False
            async for chunk in response.aiter_bytes():
                remaining = self._max_response_bytes - len(body)
                if len(chunk) >= remaining:
                    body.extend(chunk[:remaining])
                    overflowed = True
                    break
                body.extend(chunk)
        finally:
            await response.aclose()

        decoded = bytes(body).decode(_charset(content_type), errors="replace")
        document = self._document(media_type, decoded)

        return FetchedPage(
            url=requested,
            final_url=target.url,
            status_code=response.status_code,
            content_type=media_type,
            title=document.title,
            text=document.text,
            truncated=document.truncated or overflowed,
            redirects=redirects,
        )

    def _document(self, media_type: str, decoded: str) -> ExtractedDocument:
        if media_type in HTML_TYPES:
            return extract_text(decoded, max_characters=self._max_characters)
        return extract_plain_text(decoded, max_characters=self._max_characters)
