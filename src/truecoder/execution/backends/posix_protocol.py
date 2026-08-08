from __future__ import annotations

import asyncio
import json
import os
import struct
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from .posix_plan import POSIX_PROTOCOL_VERSION

FrameType: TypeAlias = Literal[
    "CONFIG",
    "READY",
    "START",
    "STARTED",
    "TERMINATE",
    "EXIT",
    "ERROR",
    "CHILD_READY",
]

FRAME_TYPES: Final = frozenset(
    {
        "CONFIG",
        "READY",
        "START",
        "STARTED",
        "TERMINATE",
        "EXIT",
        "ERROR",
        "CHILD_READY",
    }
)
MAX_POSIX_FRAME_BYTES: Final = 256 * 1024
_HEADER = struct.Struct("!I")
_FRAME_KEYS: Final = {"version", "type", "payload"}
_PAYLOAD_KEYS: Final = {
    "READY": {"supervisor_pid", "project_pgid"},
    "START": set(),
    "STARTED": {"project_pid"},
    "TERMINATE": {"reason", "grace_seconds"},
    "EXIT": {"exit_code", "signal", "native_reason"},
    "ERROR": {"operation", "code", "message", "command_started"},
    "CHILD_READY": {"project_pid"},
}


@dataclass(frozen=True, slots=True)
class PosixFrame:
    type: FrameType
    payload: dict[str, object]

    def __post_init__(self) -> None:
        _validate_frame(self.type, self.payload)


def encode_frame(
    frame_type: FrameType,
    payload: dict[str, object],
) -> bytes:
    _validate_frame(frame_type, payload)
    body = json.dumps(
        {
            "version": POSIX_PROTOCOL_VERSION,
            "type": frame_type,
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not body or len(body) > MAX_POSIX_FRAME_BYTES:
        raise ValueError("POSIX protocol frame exceeds its size limit")
    return _HEADER.pack(len(body)) + body


def decode_frame(data: bytes) -> PosixFrame:
    if not isinstance(data, bytes):
        raise TypeError("frame data must be bytes")
    if not data or len(data) > MAX_POSIX_FRAME_BYTES:
        raise ValueError("POSIX protocol frame has an invalid size")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("POSIX protocol frame is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _FRAME_KEYS:
        raise ValueError("POSIX protocol frame fields are invalid")
    if value["version"] != POSIX_PROTOCOL_VERSION:
        raise ValueError("POSIX protocol version is unsupported")
    frame_type = value["type"]
    payload = value["payload"]
    if not isinstance(frame_type, str) or frame_type not in FRAME_TYPES:
        raise ValueError("POSIX protocol frame type is unknown")
    if not isinstance(payload, dict):
        raise TypeError("POSIX protocol payload must be an object")
    return PosixFrame(frame_type, payload)  # type: ignore[arg-type]


def read_frame_fd(fd: int) -> PosixFrame:
    header = _read_exact_fd(fd, _HEADER.size)
    length = _HEADER.unpack(header)[0]
    if length == 0 or length > MAX_POSIX_FRAME_BYTES:
        raise ValueError("POSIX protocol frame has an invalid size")
    return decode_frame(_read_exact_fd(fd, length))


def write_frame_fd(
    fd: int,
    frame_type: FrameType,
    payload: dict[str, object],
) -> None:
    data = encode_frame(frame_type, payload)
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise BrokenPipeError("POSIX protocol pipe closed during write")
        offset += written


async def read_frame_stream(reader: asyncio.StreamReader) -> PosixFrame:
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        raise EOFError("POSIX protocol stream closed before a frame") from exc
    length = _HEADER.unpack(header)[0]
    if length == 0 or length > MAX_POSIX_FRAME_BYTES:
        raise ValueError("POSIX protocol frame has an invalid size")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise EOFError("POSIX protocol stream closed during a frame") from exc
    return decode_frame(body)


async def write_frame_async(
    fd: int,
    frame_type: FrameType,
    payload: dict[str, object],
) -> None:
    data = encode_frame(frame_type, payload)
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    offset = 0

    def write_ready() -> None:
        nonlocal offset
        try:
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise BrokenPipeError("POSIX protocol pipe closed during write")
                offset += written
        except BlockingIOError:
            return
        except OSError as exc:
            loop.remove_writer(fd)
            if not done.done():
                done.set_exception(exc)
            return
        loop.remove_writer(fd)
        if not done.done():
            done.set_result(None)

    loop.add_writer(fd, write_ready)
    try:
        await done
    finally:
        loop.remove_writer(fd)


def _read_exact_fd(fd: int, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise EOFError("POSIX protocol pipe closed during a frame")
        data.extend(chunk)
    return bytes(data)


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("POSIX protocol objects cannot contain duplicate keys")
        result[key] = value
    return result


def _validate_frame(
    frame_type: object,
    payload: object,
) -> None:
    if not isinstance(frame_type, str) or frame_type not in FRAME_TYPES:
        raise ValueError("POSIX protocol frame type is unknown")
    if not isinstance(payload, dict):
        raise TypeError("POSIX protocol payload must be an object")
    if frame_type == "CONFIG":
        return
    expected = _PAYLOAD_KEYS[frame_type]
    if set(payload) != expected:
        raise ValueError(f"{frame_type} payload fields are invalid")
    if frame_type in {"READY", "STARTED", "CHILD_READY"}:
        for value in payload.values():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{frame_type} process identifiers must be positive")
    elif frame_type == "TERMINATE":
        reason = payload["reason"]
        grace = payload["grace_seconds"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("TERMINATE reason must not be empty")
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace < 0:
            raise ValueError("TERMINATE grace_seconds must not be negative")
    elif frame_type == "EXIT":
        exit_code = payload["exit_code"]
        signal_number = payload["signal"]
        native_reason = payload["native_reason"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise TypeError("EXIT exit_code must be an integer or null")
        if signal_number is not None and (
            isinstance(signal_number, bool)
            or not isinstance(signal_number, int)
            or signal_number <= 0
        ):
            raise TypeError("EXIT signal must be a positive integer or null")
        if native_reason is not None and (
            not isinstance(native_reason, str) or not native_reason.strip()
        ):
            raise TypeError("EXIT native_reason must be a non-empty string or null")
        if exit_code is None and signal_number is None and native_reason is None:
            raise ValueError("EXIT requires an exit code, signal, or native reason")
    elif frame_type == "ERROR":
        for name in ("operation", "code", "message"):
            value = payload[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ERROR {name} must not be empty")
        if not isinstance(payload["command_started"], bool):
            raise TypeError("ERROR command_started must be a boolean")
