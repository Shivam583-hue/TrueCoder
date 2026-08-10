from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from platformdirs import user_cache_path

FALLBACK_ENCODING: Final = "o200k_base"
APPROXIMATE_BYTES_PER_TOKEN: Final = 3
WARMUP_THREAD_NAME: Final = "truecoder-tokenizer"

_CACHE_VARIABLES: Final = ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR")
_UNAVAILABLE: Final = (ImportError, OSError, ValueError)


class Encoding(Protocol):
    def encode(self, text: str) -> list[int]: ...


@runtime_checkable
class PreparesLazily(Protocol):
    def prepare(self) -> None: ...


def default_tokenizer_cache_path() -> Path:
    return user_cache_path("truecoder", appauthor=False) / "tokenizers"


def configure_tokenizer_cache(path: Path | None = None) -> None:
    if any(variable in os.environ for variable in _CACHE_VARIABLES):
        return

    target = path or default_tokenizer_cache_path()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    os.environ["TIKTOKEN_CACHE_DIR"] = str(target)


def load_encoding(model: str) -> Encoding | None:
    try:
        import tiktoken

        configure_tokenizer_cache()
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding(FALLBACK_ENCODING)
    except _UNAVAILABLE:
        return None


def approximate_tokens(text: str) -> int:
    if not text:
        return 0
    length = len(text.encode("utf-8"))
    return max(1, math.ceil(length / APPROXIMATE_BYTES_PER_TOKEN))


def warm_tokenizer(counter: object) -> threading.Thread | None:
    if not isinstance(counter, PreparesLazily):
        return None

    thread = threading.Thread(
        target=counter.prepare,
        name=WARMUP_THREAD_NAME,
        daemon=True,
    )
    thread.start()
    return thread
