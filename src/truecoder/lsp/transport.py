from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from truecoder.lsp.protocol import (
    MessageBuffer,
    ProtocolError,
    encode_message,
    notification_message,
    request_message,
    response_error,
)

DEFAULT_REQUEST_TIMEOUT: Final = 20.0
DEFAULT_STOP_TIMEOUT: Final = 5.0
READ_CHUNK_BYTES: Final = 65536
MAX_STDERR_CHARACTERS: Final = 8000

NotificationHandler = Callable[[str, dict[str, Any]], None]


class TransportError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class StdioTransport:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if not command:
            raise ValueError("A transport requires a command")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")

        self._command = tuple(command)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._request_timeout = request_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._buffer = MessageBuffer()
        self._next_id = 1
        self._handler: NotificationHandler | None = None
        self._stderr = ""

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_tail(self) -> str:
        return self._stderr

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self.running:
            return

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=self._env if self._env is not None else os.environ.copy(),
            )
        except (OSError, ValueError) as error:
            raise TransportError(
                f"The language server could not be started: {error}",
                code="start_failed",
            ) from error

        self._buffer = MessageBuffer()
        self._stderr = ""
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    async def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        process = self._process
        self._process = None

        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader, self._stderr_reader) if task is not None),
            return_exceptions=True,
        )
        self._reader = None
        self._stderr_reader = None
        self._fail_pending("The language server was stopped.", code="stopped")

        if process is None or process.returncode is not None:
            return

        try:
            process.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write(notification_message(method, params))

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.running:
            raise TransportError(
                "The language server is not running.",
                code="not_running",
            )

        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future

        try:
            await self._write(request_message(request_id, method, params))
            payload = await asyncio.wait_for(
                future,
                timeout=timeout if timeout is not None else self._request_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as error:
            raise TransportError(
                f"The language server did not answer {method} in time.",
                code="request_timeout",
            ) from error
        finally:
            self._pending.pop(request_id, None)

        described = response_error(payload)
        if described is not None:
            raise TransportError(
                f"{method} failed: {described}",
                code="request_failed",
            )
        return payload.get("result")

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise TransportError(
                "The language server is not running.",
                code="not_running",
            )

        try:
            process.stdin.write(encode_message(payload))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            raise TransportError(
                "The language server closed its input.",
                code="write_failed",
            ) from error

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        try:
            while True:
                chunk = await process.stdout.read(READ_CHUNK_BYTES)
                if not chunk:
                    self._fail_pending(
                        "The language server exited.",
                        code="server_exited",
                    )
                    return
                for payload in self._buffer.feed(chunk):
                    self._dispatch(payload)
        except asyncio.CancelledError:
            raise
        except ProtocolError as error:
            self._fail_pending(error.message, code=error.code)
        except Exception:  # noqa: BLE001 - a reader failure must not escape the task
            self._fail_pending(
                "The language server connection failed.",
                code="read_failed",
            )

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return

        try:
            while True:
                chunk = await process.stderr.read(READ_CHUNK_BYTES)
                if not chunk:
                    return
                text = self._stderr + chunk.decode("utf-8", errors="replace")
                self._stderr = text[-MAX_STDERR_CHARACTERS:]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - diagnostics must never break the session
            return

    def _dispatch(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        method = payload.get("method")

        if method is None and isinstance(request_id, int):
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        if method is not None and request_id is None:
            handler = self._handler
            if handler is not None:
                params = payload.get("params")
                handler(str(method), params if isinstance(params, dict) else {})
            return

        if method is not None and request_id is not None:
            asyncio.create_task(self._refuse(request_id, str(method)))

    async def _refuse(self, request_id: Any, method: str) -> None:
        del method
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": None,
                }
            )
        except TransportError:
            return

    def _fail_pending(self, message: str, *, code: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(TransportError(message, code=code))
        self._pending.clear()
