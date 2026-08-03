from __future__ import annotations

from typing import Final

from ..errors import BackendUnavailableError
from ..models import BackendName, BackendPreference
from .base import ExecutionBackend
from .models import BackendDescriptor

_PREFERENCE_BY_NAME: Final[dict[BackendName, BackendPreference]] = {
    "posix": "local",
    "windows": "local",
    "container": "container",
}


class BackendRegistry:
    def __init__(self, backends: tuple[ExecutionBackend, ...]) -> None:
        _validate_backends(backends)

        registered: dict[BackendName, ExecutionBackend] = {}
        for backend in backends:
            name = backend.descriptor.name
            if name in registered:
                raise ValueError(f"duplicate backend name: {name!r}")
            registered[name] = backend

        self._backends = registered

    def get_exact(
        self,
        descriptor: BackendDescriptor,
        *,
        execution_id: str | None = None,
    ) -> ExecutionBackend:
        _validate_descriptor(descriptor)

        backend = self._backends.get(descriptor.name)
        if backend is None:
            raise BackendUnavailableError(
                f"backend {descriptor.name!r} is not registered",
                preference=_PREFERENCE_BY_NAME[descriptor.name],
                execution_id=execution_id,
            )

        if backend.descriptor != descriptor:
            raise BackendUnavailableError(
                f"backend {descriptor.name!r} no longer matches the descriptor "
                "selected during preparation",
                preference=_PREFERENCE_BY_NAME[descriptor.name],
                execution_id=execution_id,
            )

        return backend


def _validate_backends(backends: object) -> None:
    if not isinstance(backends, tuple):
        raise TypeError("backends must be a tuple")
    if any(not isinstance(backend, ExecutionBackend) for backend in backends):
        raise TypeError("backends must contain ExecutionBackend values")


def _validate_descriptor(descriptor: object) -> None:
    if not isinstance(descriptor, BackendDescriptor):
        raise TypeError("descriptor must be a BackendDescriptor")
