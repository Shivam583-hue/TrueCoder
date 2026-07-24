import asyncio

from truecoder.tools.base import (
    ToolApproval,
    ToolArgumentError,
    ToolCall,
    ToolResult,
)
from truecoder.tools.registry import ToolNotFoundError, ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
    ) -> ToolResult:
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

        if tool.approval is ToolApproval.REQUIRED and approved is not True:
            return ToolResult.approval_required(
                call_id=call.call_id,
                tool_name=call.name,
            )

        try:
            output = await tool.run(arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
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
