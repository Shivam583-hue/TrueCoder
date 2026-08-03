import asyncio
from dataclasses import dataclass
from typing import Any

from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArgumentError,
    ToolArguments,
    ToolCall,
    ToolExecutionError,
    ToolResult,
)
from truecoder.tools.context import ToolInvocationContext
from truecoder.tools.registry import ToolNotFoundError, ToolRegistry


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """A tool call whose tool and arguments have already been validated."""

    call: ToolCall
    tool: BaseTool[Any]
    arguments: ToolArguments


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
        invocation: ToolInvocationContext | None = None,
    ) -> ToolResult:
        prepared = self.prepare(call)
        if isinstance(prepared, ToolResult):
            return prepared
        return await self.execute_prepared(
            prepared,
            approved=approved,
            invocation=invocation,
        )

    def prepare(self, call: ToolCall) -> PreparedToolCall | ToolResult:
        """Resolve and validate a call without requesting approval or running it."""

        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        try:
            tool = self.registry.get(call.name)
        except ToolNotFoundError as error:
            return ToolResult.failure(
                call_id=call.call_id,
                tool_name=call.name,
                error=str(error),
                error_code="tool_not_found",
            )

        try:
            arguments = tool.parse_arguments(call.arguments_json)
        except ToolArgumentError as error:
            return ToolResult.failure(
                call_id=call.call_id,
                tool_name=call.name,
                error=str(error),
                error_code="invalid_arguments",
            )

        return PreparedToolCall(call=call, tool=tool, arguments=arguments)

    async def execute_prepared(
        self,
        prepared: PreparedToolCall,
        *,
        approved: bool = False,
        invocation: ToolInvocationContext | None = None,
    ) -> ToolResult:
        """Run exactly the tool and validated arguments approved by the caller."""

        if not isinstance(prepared, PreparedToolCall):
            raise TypeError("prepared must be a PreparedToolCall")

        call = prepared.call
        tool = prepared.tool
        if tool.approval is ToolApproval.REQUIRED and approved is not True:
            return ToolResult.approval_required(
                call_id=call.call_id,
                tool_name=call.name,
            )

        try:
            output = await tool.run(prepared.arguments, invocation)
        except asyncio.CancelledError:
            raise
        except ToolExecutionError as error:
            return ToolResult.failure(
                call_id=call.call_id,
                tool_name=call.name,
                error=error.message,
                error_code=error.code,
            )
        except Exception as error:  # noqa: BLE001 - return unexpected tool failures
            return ToolResult.failure(
                call_id=call.call_id,
                tool_name=call.name,
                error=str(error),
                error_code="execution_failed",
            )

        return ToolResult.success(
            call_id=call.call_id,
            tool_name=call.name,
            output=output,
        )
