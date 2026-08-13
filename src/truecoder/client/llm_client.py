import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from dotenv import load_dotenv
from httpx import TimeoutException, TransportError
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AsyncStream,
    RateLimitError,
)
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage

from truecoder.client.failures import (
    ProviderFailure,
    classify_exception,
    timed_out,
    unreachable,
)
from truecoder.client.native import NativeProviderError, native_completion
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
)
from truecoder.client.responses import (
    non_stream_response,
    responses_request,
    stream_response,
)
from truecoder.providers.models import (
    Credential,
    CredentialError,
    SessionSettings,
    resolve_settings,
)
from truecoder.providers.oauth import OAuthError, OAuthToken, refresh_token
from truecoder.tools.base import ToolCall

load_dotenv()


@dataclass(slots=True)
class _ToolCallBuffer:
    call_id: str | None = None
    name: str | None = None
    argument_fragments: list[str] = field(default_factory=list)


class LLMClient:
    def __init__(self, settings: SessionSettings | None = None) -> None:
        self.__client: AsyncOpenAI | None = None
        self._max_retries: int = 3
        self._settings = settings
        self._refresh_lock = asyncio.Lock()
        if settings is not None:
            settings.on_connection_change(self._invalidate)

    @property
    def settings(self) -> SessionSettings:
        if self._settings is None:
            try:
                resolved = resolve_settings()
            except CredentialError as error:
                raise RuntimeError(str(error)) from None
            resolved.on_connection_change(self._invalidate)
            self._settings = resolved
        return self._settings

    def _invalidate(self) -> None:
        client = self.__client
        self.__client = None
        if client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(client.close())

    async def __aenter__(self) -> "LLMClient":  # noqa: PYI034 - Python 3.10
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def get_client(self) -> AsyncOpenAI:
        if self.__client is None:
            settings = self.settings
            if settings.credential is None:
                provider = settings.provider
                name = provider.label if provider.is_named else "The model provider"
                if provider.oauth is not None:
                    guidance = (
                        f"{name} isn't connected. Open /models, then choose "
                        f"{name} to sign in or add an API key."
                    )
                else:
                    guidance = (
                        f"{name} doesn't have an API key. Open /models, then "
                        f"choose {name} to add one."
                    )
                raise RuntimeError(guidance)

            client_options: dict[str, Any] = {
                **settings.credential.client_options(),
                "max_retries": 0,
            }
            base_url = (
                settings.credential.endpoint_override() or settings.provider.base_url
            )
            if base_url:
                client_options["base_url"] = base_url

            headers = dict(settings.provider.headers)
            headers.update(settings.credential.request_headers())
            if headers:
                client_options["default_headers"] = headers

            self.__client = AsyncOpenAI(**client_options)

        return self.__client

    async def close(self) -> None:
        if self.__client is not None:
            await self.__client.close()
            self.__client = None

    @staticmethod
    def _is_stale(credential: Credential | None) -> bool:
        if not isinstance(credential, OAuthToken):
            return False
        return credential.needs_refresh and credential.refresh_token is not None

    async def refresh_credential(self) -> bool:
        settings = self.settings
        if not self._is_stale(settings.credential):
            return False
        client = settings.provider.oauth
        if client is None:
            return False

        async with self._refresh_lock:
            current = settings.credential
            if not self._is_stale(current):
                return False
            assert isinstance(current, OAuthToken)
            try:
                refreshed = await refresh_token(client, current)
            except OAuthError:
                return False
            settings.use(settings.provider, refreshed)
            self._persist(refreshed)
            return True

    @staticmethod
    def _persist(token: OAuthToken) -> None:
        from truecoder.providers.tokens import store_token

        store_token(token)

    async def chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        stream: bool = True,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        await self.refresh_credential()
        settings = self.settings
        model = settings.model
        native = settings.provider.adapter in {"anthropic", "google"}
        client = None if native else self.get_client()

        if native:
            request = {}
        elif settings.provider.wire_api == "responses":
            request = responses_request(model, messages, tools)
        elif tools:
            request = {
                "model": model,
                "tools": tools,
                "messages": messages,
            }
        else:
            request = {
                "model": model,
                "messages": messages,
            }

        for attempt in range(self._max_retries + 1):
            stream_started = False

            try:
                if native:
                    assert settings.credential is not None
                    async for event in native_completion(
                        settings.provider,
                        settings.credential,
                        model,
                        messages,
                        tools,
                        stream=stream,
                    ):
                        stream_started = (
                            stream_started
                            or event.type != EventType.MESSAGE_COMPLETE
                        )
                        yield event
                elif stream:
                    assert client is not None
                    events = (
                        stream_response(client, request)
                        if settings.provider.wire_api == "responses"
                        else self._stream_response(client, request)
                    )
                    async for event in events:
                        stream_started = True
                        yield event
                else:
                    assert client is not None
                    if settings.provider.wire_api == "responses":
                        yield await non_stream_response(client, request)
                    else:
                        yield await self._non_stream_response(client, request)
                return
            except RateLimitError as error:
                if not stream_started and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._failure_event(error, partial=stream_started)
                return
            except NativeProviderError as error:
                if (
                    error.status_code == 429
                    and not stream_started
                    and attempt < self._max_retries
                ):
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._failure_event(error, partial=stream_started)
                return
            except APITimeoutError:
                if not stream_started and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._reported(timed_out(self._provider_name(), partial=stream_started))
                return
            except APIConnectionError as error:
                if not stream_started and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._reported(
                    unreachable(
                        self._provider_name(),
                        str(error),
                        partial=stream_started,
                    )
                )
                return
            except TimeoutException:
                if not stream_started and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._reported(
                    timed_out(self._provider_name(), partial=stream_started)
                )
                return
            except TransportError as error:
                if not stream_started and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield self._reported(
                    unreachable(
                        self._provider_name(),
                        str(error),
                        partial=stream_started,
                    )
                )
                return
            except APIError as error:
                yield self._failure_event(error, partial=stream_started)
                return

    def _provider_name(self) -> str:
        try:
            return self.settings.provider.name
        except (RuntimeError, CredentialError):
            return ""

    def _failure_event(self, error: object, *, partial: bool) -> StreamEvent:
        try:
            model = self.settings.model
        except (RuntimeError, CredentialError):
            model = ""
        return self._reported(
            classify_exception(
                error,
                provider=self._provider_name(),
                model=model,
                partial=partial,
            )
        )

    @staticmethod
    def _reported(failure: ProviderFailure) -> StreamEvent:
        return StreamEvent(
            type=EventType.ERROR,
            error=failure.message,
            failure=failure,
        )

    async def _stream_response(
        self,
        client: AsyncOpenAI,
        request: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        response = cast(
            AsyncStream[ChatCompletionChunk],
            await client.chat.completions.create(
                **request,
                stream=True,
                stream_options={"include_usage": True},
            ),
        )

        usage: TokenUsage | None = None
        finish_reason: str | None = None
        tool_call_buffers: dict[int, _ToolCallBuffer] = {}

        async with response:
            async for chunk in response:
                if chunk.usage is not None:
                    usage = self._to_token_usage(chunk.usage)

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]

                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason

                if choice.delta.content:
                    yield StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta(
                            content=choice.delta.content,
                        ),
                    )

                for fragment in choice.delta.tool_calls or []:
                    index = fragment.index

                    if not isinstance(index, int) or index < 0:
                        yield StreamEvent(
                            type=EventType.ERROR,
                            error=(
                                "The provider returned a tool-call fragment "
                                "without a valid index."
                            ),
                        )
                        return

                    buffer = tool_call_buffers.setdefault(
                        index,
                        _ToolCallBuffer(),
                    )

                    if fragment.id is not None:
                        if buffer.call_id is not None and buffer.call_id != fragment.id:
                            yield StreamEvent(
                                type=EventType.ERROR,
                                error=(
                                    f"Tool call at index {index} returned "
                                    "conflicting call IDs."
                                ),
                            )
                            return

                        buffer.call_id = fragment.id

                    function = fragment.function
                    name = function.name if function is not None else None
                    arguments_fragment = (
                        function.arguments if function is not None else None
                    )

                    if name is not None:
                        if buffer.name is not None and buffer.name != name:
                            yield StreamEvent(
                                type=EventType.ERROR,
                                error=(
                                    f"Tool call at index {index} returned "
                                    "conflicting function names."
                                ),
                            )
                            return

                        buffer.name = name

                    if arguments_fragment is not None:
                        buffer.argument_fragments.append(arguments_fragment)

                    yield StreamEvent(
                        type=EventType.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index=index,
                            call_id=fragment.id,
                            name=name,
                            arguments_delta=arguments_fragment or "",
                        ),
                    )

        completed_tool_calls: list[ToolCall] = []

        for index in sorted(tool_call_buffers):
            buffer = tool_call_buffers[index]

            if not buffer.call_id:
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=(f"Tool call at index {index} completed without a call ID."),
                )
                return

            if not buffer.name:
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=(
                        f"Tool call at index {index} completed without a function name."
                    ),
                )
                return

            arguments_json = "".join(buffer.argument_fragments)

            completed_tool_calls.append(
                ToolCall(
                    call_id=buffer.call_id,
                    name=buffer.name,
                    arguments_json=arguments_json,
                )
            )

        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tuple(completed_tool_calls),
        )

    async def _non_stream_response(
        self,
        client: AsyncOpenAI,
        request: dict[str, Any],
    ) -> StreamEvent:
        response = cast(
            ChatCompletion,
            await client.chat.completions.create(
                **request,
                stream=False,
            ),
        )

        usage = self._to_token_usage(response.usage)

        if not response.choices:
            return StreamEvent(
                type=EventType.ERROR,
                error="The model returned a response without any choices.",
                usage=usage,
            )

        choice = response.choices[0]

        text_delta = (
            TextDelta(content=choice.message.content)
            if choice.message.content
            else None
        )

        tool_calls: list[ToolCall] = []

        for sdk_call in choice.message.tool_calls or []:
            try:
                tool_call = ToolCall(
                    call_id=sdk_call.id,
                    name=sdk_call.function.name,
                    arguments_json=sdk_call.function.arguments,
                )
            except (TypeError, ValueError) as error:
                return StreamEvent(
                    type=EventType.ERROR,
                    error=f"The provider returned an invalid tool call: {error}",
                    usage=usage,
                )

            tool_calls.append(tool_call)

        return StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            tool_calls=tuple(tool_calls),
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    @staticmethod
    def _to_token_usage(usage: CompletionUsage | None) -> TokenUsage | None:
        if usage is None:
            return None

        prompt_details = usage.prompt_tokens_details
        cached_tokens = (
            prompt_details.cached_tokens
            if prompt_details is not None and prompt_details.cached_tokens is not None
            else 0
        )

        return TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=cached_tokens,
        )
