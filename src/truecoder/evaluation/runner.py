from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from truecoder.agent.events import AgentEventType
from truecoder.evaluation.models import (
    MAX_DETAIL_CHARACTERS,
    EvalReport,
    EvalResult,
    EvalTask,
)

AgentFactory = Callable[[Path, EvalTask], object]


def materialise(task: EvalTask, root: Path) -> None:
    for relative, content in task.files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def run_task(task: EvalTask, build_agent: AgentFactory) -> EvalResult:
    if not isinstance(task, EvalTask):
        raise TypeError("task must be an EvalTask")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        materialise(task, root)

        reply = ""
        tool_calls = 0
        failure: str | None = None
        agent = None

        try:
            agent = build_agent(root, task)
            async for event in agent.run(task.prompt):
                if event.type is AgentEventType.TOOL_CALL:
                    tool_calls += 1
                elif event.type is AgentEventType.AGENT_ERROR:
                    failure = str(event.data.get("error", "the turn failed"))
                elif event.type is AgentEventType.AGENT_END:
                    reply = str(event.data.get("response") or "")
        except Exception as error:  # noqa: BLE001 - one task never stops the suite
            failure = f"{type(error).__name__}: {error}"
        finally:
            closer = getattr(agent, "close", None)
            if callable(closer):
                await closer()

        if failure is None and task.check is not None:
            failure = task.check(root)

    return EvalResult(
        task=task.name,
        passed=failure is None,
        detail=None if failure is None else failure[:MAX_DETAIL_CHARACTERS],
        reply=reply,
        tool_calls=tool_calls,
    )


async def run_suite(
    tasks: Sequence[EvalTask],
    build_agent: AgentFactory,
) -> EvalReport:
    results = [await run_task(task, build_agent) for task in tasks]
    return EvalReport(results=tuple(results))
