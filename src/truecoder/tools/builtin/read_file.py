from abc import abstractmethod
from typing import TypedDict

from pydantic import Field

from truecoder.tools.base import BaseTool, ToolArguments


class ReadFileArguments(ToolArguments):
    """Validated arguments accepted by the read-file tool."""

    path: str = Field(
        min_length=1,
        description="Path to the file, relative to the workspace.",
    )
    start_line: int = Field(
        ge=1,
        description="One-based line number at which to start reading.",
    )
    line_count: int = Field(
        ge=1,
        description="Maximum number of lines to return.",
    )


class ReadFileOutput(TypedDict):
    """Structured output returned by the read file tool."""

    path: str
    content: str
    start_line: int
    end_line: int
    has_more: bool


class ReadFileTool(BaseTool[ReadFileArguments]):
    """Contract for a workspace bound, line oriented file reader."""

    name = "read_file"
    description = "Read a range of lines from a file in the workspace."
    arguments_type = ReadFileArguments

    @abstractmethod
    async def run(self, arguments: ReadFileArguments) -> ReadFileOutput:
        """Read the requested range and return its actual line boundaries."""

        raise NotImplementedError
