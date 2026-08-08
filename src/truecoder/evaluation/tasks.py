from __future__ import annotations

from typing import Final

from truecoder.evaluation.models import EvalTask, all_of, file_contains, file_unchanged

CALCULATOR: Final = "def add(a, b):\n    return a - b\n"
NOTES: Final = "# Notes\n\nnothing yet\n"

DEFAULT_TASKS: Final = (
    EvalTask(
        name="fix-the-obvious-bug",
        prompt=(
            "calc.py has a bug: add returns the difference instead of the sum. Fix it."
        ),
        files={"calc.py": CALCULATOR},
        check=file_contains("calc.py", "a + b"),
    ),
    EvalTask(
        name="answer-without-changing-anything",
        prompt="What does add in calc.py currently return? Do not change any file.",
        files={"calc.py": CALCULATOR, "notes.md": NOTES},
        check=all_of(
            file_unchanged("calc.py", CALCULATOR),
            file_unchanged("notes.md", NOTES),
        ),
    ),
    EvalTask(
        name="two-edits-in-one-file",
        prompt=(
            "In calc.py, fix add to return the sum and rename the parameters "
            "from a and b to left and right."
        ),
        files={"calc.py": CALCULATOR},
        check=all_of(
            file_contains("calc.py", "left"),
            file_contains("calc.py", "right"),
            file_contains("calc.py", "left + right"),
        ),
    ),
    EvalTask(
        name="create-a-file-that-was-asked-for",
        prompt="Create a file called VERSION containing exactly the text 1.0.0",
        files={"calc.py": CALCULATOR},
        check=file_contains("VERSION", "1.0.0"),
    ),
)
