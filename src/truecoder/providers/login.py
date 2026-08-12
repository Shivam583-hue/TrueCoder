from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from truecoder.providers.oauth import (
    CALLBACK_HOST,
    CALLBACK_TIMEOUT_SECONDS,
    FAILURE_BODY,
    MAX_CALLBACK_BYTES,
    SUCCESS_BODY,
    CallbackResult,
    OAuthClient,
    OAuthError,
    OAuthToken,
    authorization_url,
    exchange_body,
    generate_pkce,
    generate_state,
    parse_token_response,
    post_token,
    read_callback,
)

CALLBACK_PATH: Final = "/callback"


def http_response(body: str, *, status: str = "200 OK") -> bytes:
    payload = body.encode("utf-8")
    header = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return header.encode("ascii") + payload


def request_target(request_line: str) -> str:
    parts = request_line.split(" ")
    return parts[1] if len(parts) >= 2 else ""


@dataclass(slots=True)
class CallbackServer:
    expected_state: str
    port: int = 0
    result: CallbackResult | None = None
    _server: asyncio.Server | None = None
    _done: asyncio.Event | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{CALLBACK_HOST}:{self.port}{CALLBACK_PATH}"

    async def start(self) -> None:
        self._done = asyncio.Event()
        try:
            self._server = await asyncio.start_server(
                self._handle,
                host=CALLBACK_HOST,
                port=self.port,
            )
        except OSError as error:
            if self.port == 0:
                raise OAuthError(f"the callback port could not be opened: {error}") from None
            raise OAuthError(
                f"port {self.port} is required by this provider but is already in use"
            ) from None
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readline(),
                timeout=10.0,
            )
            line = raw[:MAX_CALLBACK_BYTES].decode("latin-1", errors="replace").strip()
            outcome = read_callback(request_target(line), self.expected_state)
            if self.result is None:
                self.result = outcome
            body = SUCCESS_BODY if outcome.error is None else FAILURE_BODY
            status = "200 OK" if outcome.error is None else "400 Bad Request"
            writer.write(http_response(body, status=status))
            await writer.drain()
        except Exception:  # noqa: BLE001 - a bad callback must not break the flow
            if self.result is None:
                self.result = CallbackResult(error="the callback could not be read")
        finally:
            writer.close()
            if self._done is not None:
                self._done.set()

    async def wait(
        self, *, timeout: float = CALLBACK_TIMEOUT_SECONDS
    ) -> CallbackResult:
        assert self._done is not None
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except TimeoutError:
            return CallbackResult(error="the browser did not return in time")
        return self.result or CallbackResult(error="no authorisation was received")

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:  # noqa: BLE001 - shutdown never raises
            return


@dataclass(slots=True)
class PendingLogin:
    client: OAuthClient
    provider: str
    server: CallbackServer
    verifier: str
    url: str

    async def wait(
        self, *, timeout: float = CALLBACK_TIMEOUT_SECONDS
    ) -> OAuthToken:
        outcome = await self.server.wait(timeout=timeout)
        if outcome.error is not None:
            raise OAuthError(outcome.error)
        assert outcome.code is not None

        payload = await post_token(
            self.client,
            exchange_body(
                self.client,
                code=outcome.code,
                redirect_uri=self.server.redirect_uri,
                verifier=self.verifier,
            ),
        )
        return parse_token_response(
            payload,
            provider=self.provider,
            client=self.client,
        )

    async def close(self) -> None:
        await self.server.stop()


async def begin_login(client: OAuthClient, *, provider: str = "") -> PendingLogin:
    if not isinstance(client, OAuthClient):
        raise OAuthError("client must be an OAuthClient")

    pkce = generate_pkce()
    state = generate_state()
    server = CallbackServer(expected_state=state, port=client.redirect_port)
    await server.start()

    try:
        url = authorization_url(
            client,
            redirect_uri=server.redirect_uri,
            pkce=pkce,
            state=state,
        )
    except BaseException:
        await server.stop()
        raise

    return PendingLogin(
        client=client,
        provider=provider,
        server=server,
        verifier=pkce.verifier,
        url=url,
    )


async def authorise(
    client: OAuthClient,
    *,
    provider: str = "",
    open_browser=None,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> OAuthToken:
    pending = await begin_login(client, provider=provider)
    try:
        opener = open_browser or open_in_browser
        opener(pending.url)
        return await pending.wait(timeout=timeout)
    finally:
        await pending.close()


def open_in_browser(target: str) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(target))
    except Exception:  # noqa: BLE001 - a missing browser must not end the flow
        return False
