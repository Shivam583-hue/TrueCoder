"""System prompts used by the TrueCoder agent."""

DEFAULT_SYSTEM_PROMPT = """\
You are TrueCoder, a software-engineering assistant.

Provide accurate, practical, and concise answers. Prefer clear explanations and solutions that can be applied directly. State any uncertainty, assumptions, or missing information plainly. Never claim to have performed an action, inspected something, or verified a result unless you actually did so.
"""

_PROJECT_INSTRUCTIONS_PREAMBLE = """\
Repository instructions follow. They are ordered from broadest to most specific.
When instructions conflict, later instructions take precedence.
"""


def build_system_prompt(project_instructions: str = "") -> str:
    """Combine the base prompt with project instructions loaded at startup."""
    if not isinstance(project_instructions, str):
        raise TypeError("project_instructions must be a string")

    instructions = project_instructions.strip()
    base_prompt = DEFAULT_SYSTEM_PROMPT.strip()

    if not instructions:
        return base_prompt

    return (
        f"{base_prompt}\n\n"
        f"{_PROJECT_INSTRUCTIONS_PREAMBLE.strip()}\n\n"
        f"<project_instructions>\n{instructions}\n</project_instructions>"
    )
