from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final, TextIO

from truecoder.agent.autonomy import Autonomy, UnattendedApprovals, autonomy_from_name
from truecoder.agent.events import AgentEventType

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2

MAX_LINE_CHARACTERS: Final = 2000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="truecoder",
        description="Run TrueCoder interactively, or once against a prompt.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        help="Run this prompt without the interface and print the reply.",
    )
    parser.add_argument(
        "--autonomy",
        default=Autonomy.READ_ONLY.value,
        help=(
            "What may proceed with nobody watching: "
            f"{', '.join(level.value for level in Autonomy)}. "
            f"Defaults to {Autonomy.READ_ONLY.value}."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop the turn after this many model requests.",
    )
    parser.add_argument(
        "--eval",
        dest="run_eval",
        action="store_true",
        help="Score the agent on the shipped tasks and report how many passed.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the final reply.",
    )
    return parser


def _bounded(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_LINE_CHARACTERS:
        return f"{collapsed[: MAX_LINE_CHARACTERS - 1]}…"
    return collapsed


def render_event(event, *, quiet: bool, stream: TextIO) -> None:
    if event.type is AgentEventType.TOOL_CALL:
        if not quiet:
            name = event.data.get("name", "tool")
            print(f"· {name}", file=stream, flush=True)
        return

    if event.type is AgentEventType.TOOL_RESULT:
        if not quiet and event.data.get("status") == "error":
            detail = _bounded(str(event.data.get("content", "")))
            print(f"! {event.data.get('tool_name', 'tool')}: {detail}", file=stream)
        return

    if event.type is AgentEventType.AGENT_ERROR:
        print(f"error: {event.data.get('error', 'the turn failed')}", file=stream)


async def run_prompt(
    agent, prompt: str, *, quiet: bool, stream: TextIO
) -> tuple[str, bool]:
    reply = ""
    failed = False

    async for event in agent.run(prompt):
        if event.type is AgentEventType.AGENT_ERROR:
            failed = True
        if event.type is AgentEventType.AGENT_END:
            reply = str(event.data.get("response") or "")
        render_event(event, quiet=quiet, stream=stream)

    return reply, failed


def _report_refusals(handler: UnattendedApprovals, stream: TextIO) -> None:
    for tool_name, reason in handler.refused:
        print(f"refused {tool_name}: {reason}", file=stream)


def _eval_agent_factory(autonomy: Autonomy):
    from truecoder.agent.agent import build_eval_agent

    def build(root, task):
        agent = build_eval_agent(root, max_iterations=task.max_iterations)
        agent.approval_handler = UnattendedApprovals(autonomy)
        return agent

    return build


async def _evaluate(arguments, *, stream: TextIO) -> int:
    from truecoder.evaluation import DEFAULT_TASKS, run_suite

    autonomy = autonomy_from_name(arguments.autonomy)
    report = await run_suite(DEFAULT_TASKS, _eval_agent_factory(autonomy))

    for result in report.results:
        print(result.summary, file=stream)
    print(report.summary)
    return EXIT_OK if report.is_clean else EXIT_FAILED


async def _headless(arguments, *, stream: TextIO) -> int:
    from truecoder.agent.agent import build_session

    autonomy = autonomy_from_name(arguments.autonomy)
    handler = UnattendedApprovals(autonomy)
    session = build_session(max_iterations=arguments.max_iterations)
    agent = session.agent
    agent.approval_handler = handler

    try:
        await agent.initialize_execution()
        await agent.initialize_mcp()
        reply, failed = await run_prompt(
            agent,
            arguments.prompt,
            quiet=arguments.quiet,
            stream=stream,
        )
    finally:
        await session.close()

    _report_refusals(handler, stream)
    if reply:
        print(reply)
    return EXIT_FAILED if failed else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        autonomy_from_name(arguments.autonomy)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE

    if arguments.run_eval:
        return asyncio.run(_evaluate(arguments, stream=sys.stderr))

    if arguments.prompt is None:
        from truecoder.agent.agent import run_interactive

        run_interactive()
        return EXIT_OK

    if not arguments.prompt.strip():
        print("error: the prompt cannot be empty", file=sys.stderr)
        return EXIT_USAGE

    if arguments.max_iterations is not None and arguments.max_iterations < 1:
        print("error: --max-iterations must be at least one", file=sys.stderr)
        return EXIT_USAGE

    return asyncio.run(_headless(arguments, stream=sys.stderr))


def run() -> None:
    raise SystemExit(main())
