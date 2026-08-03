from __future__ import annotations

import json
from typing import Final

from .container_models import (
    ContainerCreatePlan,
    ContainerInspection,
    ContainerState,
)

MAX_INSPECT_BYTES: Final = 1024 * 1024
MAX_INSPECT_LABELS: Final = 64

FORBIDDEN_ARGUMENTS: Final = frozenset(
    {
        "--privileged",
        "--pid=host",
        "--ipc=host",
        "--uts=host",
        "--userns=host",
        "--network=host",
        "--net=host",
        "--cap-add",
        "--device",
        "--volumes-from",
        "--publish",
        "-p",
        "--publish-all",
        "-P",
        "--restart=always",
        "--rm",
        "--gpus",
        "--pull=always",
    }
)

_DOCKER_STATES: Final[dict[str, ContainerState]] = {
    "created": "created",
    "running": "running",
    "paused": "paused",
    "restarting": "restarting",
    "removing": "removing",
    "exited": "exited",
    "dead": "dead",
}


def docker_create_argv(plan: ContainerCreatePlan) -> tuple[str, ...]:
    if not isinstance(plan, ContainerCreatePlan):
        raise TypeError("plan must be a ContainerCreatePlan")

    security = plan.security
    argv: list[str] = [
        "create",
        "--name",
        plan.name,
        "--pull",
        "never",
        "--restart",
        "no",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--security-opt",
        f"seccomp={security.seccomp_profile}",
        "--user",
        plan.image.user,
        "--workdir",
        str(plan.workdir),
        "--memory",
        str(security.memory_bytes),
        "--memory-swap",
        str(security.memory_bytes),
        "--pids-limit",
        str(security.pids_limit),
    ]

    if security.network_mode == "none":
        argv.extend(("--network", "none"))
    else:
        argv.extend(("--network", "isolated"))

    if security.cpu_rate is not None:
        argv.extend(("--cpus", _format_rate(security.cpu_rate)))

    for key, value in plan.labels.as_pairs():
        argv.extend(("--label", f"{key}={value}"))

    for mount in plan.mounts:
        specification = (
            f"type=bind,src={mount.source},dst={mount.target}"
            + (",readonly" if mount.read_only else "")
        )
        argv.extend(("--mount", specification))

    for entry in security.tmpfs:
        options = (
            "rw,noexec,nosuid,nodev,"
            f"size={entry.size_bytes},uid={entry.uid},gid={entry.gid}"
        )
        argv.extend(("--tmpfs", f"{entry.target}:{options}"))

    if plan.env_file is not None:
        argv.extend(("--env-file", str(plan.env_file)))

    argv.append(plan.image.reference)
    argv.extend(plan.argv)

    _reject_forbidden(argv)
    return tuple(argv)


def docker_start_attach_argv(container_id: str) -> tuple[str, ...]:
    return ("start", "--attach", _exact_id(container_id))


def docker_inspect_argv(container_id: str) -> tuple[str, ...]:
    return ("inspect", "--type", "container", _exact_id(container_id))


def docker_stop_argv(container_id: str, grace_seconds: float) -> tuple[str, ...]:
    if grace_seconds < 0:
        raise ValueError("grace_seconds must not be negative")
    return (
        "stop",
        "--timeout",
        str(max(0, int(grace_seconds))),
        _exact_id(container_id),
    )


def docker_kill_argv(container_id: str) -> tuple[str, ...]:
    return ("kill", "--signal", "KILL", _exact_id(container_id))


def docker_remove_argv(container_id: str, *, force: bool) -> tuple[str, ...]:
    argv = ["rm"]
    if force:
        argv.append("--force")
    argv.append(_exact_id(container_id))
    return tuple(argv)


def docker_list_managed_argv(label: str) -> tuple[str, ...]:
    return (
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"label={label}",
        "--format",
        "{{.ID}}",
    )


def parse_docker_inspect(payload: str) -> ContainerInspection:
    if not isinstance(payload, str):
        raise TypeError("payload must be a string")
    if len(payload.encode("utf-8")) > MAX_INSPECT_BYTES:
        raise ValueError("inspect output exceeds the supported size")

    document = json.loads(payload)
    if not isinstance(document, list) or len(document) != 1:
        raise ValueError("inspect must describe exactly one container")

    entry = _as_object(document[0], "inspect entry")

    container_id = _required_text(entry, "Id")
    state = _required_object(entry, "State")
    config = _required_object(entry, "Config")

    status = _required_text(state, "Status")
    if status not in _DOCKER_STATES:
        raise ValueError(f"unknown container status: {status!r}")

    raw_labels = _as_object(config.get("Labels") or {}, "container labels")
    if len(raw_labels) > MAX_INSPECT_LABELS:
        raise ValueError("container reports too many labels")
    labels = tuple(
        (str(key), str(value))
        for key, value in sorted(raw_labels.items())
        if isinstance(key, str) and isinstance(value, str)
    )

    exit_code = state.get("ExitCode")
    if exit_code is not None and not isinstance(exit_code, int):
        raise ValueError("ExitCode must be an integer")
    oom = _as_flag(state.get("OOMKilled", False))
    error = state.get("Error")
    if error is not None and not isinstance(error, str):
        raise ValueError("Error must be a string")

    return ContainerInspection(
        container_id=container_id,
        state=_DOCKER_STATES[status],
        labels=labels,
        image_digest=_image_digest(entry),
        exit_code=None if status in {"created", "running"} else exit_code,
        oom_killed=oom,
        error=error or None,
    )


def parse_container_id(payload: str) -> str:
    return _exact_id(payload.strip())


def _image_digest(entry: dict) -> str:
    image = entry.get("Image")
    if isinstance(image, str) and image.startswith("sha256:"):
        return image
    raise ValueError("inspect did not report a pinned image digest")


def _as_object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")  # noqa: TRY004
    return value


def _as_flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("OOMKilled must be a boolean")  # noqa: TRY004
    return value


def _required_text(source: dict, key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004
            f"inspect field {key!r} is missing or malformed"
        )
    return value


def _required_object(source: dict, key: str) -> dict:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(  # noqa: TRY004
            f"inspect field {key!r} is missing or malformed"
        )
    return value


def _exact_id(container_id: str) -> str:
    if not isinstance(container_id, str):
        raise TypeError("container_id must be a string")
    candidate = container_id.strip()
    candidate = candidate.removeprefix("sha256:")
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise ValueError("container_id must be a full immutable hex ID")
    return candidate


def _format_rate(rate: float) -> str:
    return f"{rate:.3f}".rstrip("0").rstrip(".")


def _reject_forbidden(argv: list[str]) -> None:
    for argument in argv:
        if argument in FORBIDDEN_ARGUMENTS:
            raise ValueError(f"forbidden runtime argument: {argument}")
        if argument.startswith("--security-opt") and "unconfined" in argument:
            raise ValueError("security options must never be unconfined")
