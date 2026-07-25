import json

from truecoder.tools.base import ToolResult, ToolResultStatus


def serialize_tool_result(result: ToolResult) -> str:
    """Serialize a ToolResult into the JSON string handed back to the model.

    The envelope always carries the status so the model can distinguish a
    successful payload from a recoverable failure. `content` returned here is
    what `AgentState.record_tool_result` stores as the tool message body.
    """

    if result.status is ToolResultStatus.SUCCESS:
        payload: dict[str, object] = {
            "status": result.status.value,
            "output": result.output,
        }
    elif result.status is ToolResultStatus.ERROR:
        payload = {
            "status": result.status.value,
            "error": result.error,
            "error_code": result.error_code,
        }
    else:
        payload = {"status": result.status.value}

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
