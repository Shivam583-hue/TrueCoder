from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def descendant_marker(marker: Path) -> None:
    time.sleep(1.5)
    marker.write_text("survived", encoding="utf-8")


def child(marker: Path) -> None:
    grandchild = subprocess.Popen(
        [sys.executable, __file__, "descendant-marker", str(marker)]
    )
    print(grandchild.pid, flush=True)
    time.sleep(60)


def tree(marker: Path) -> None:
    descendant = subprocess.Popen(
        [sys.executable, __file__, "child", str(marker)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert descendant.stdout is not None
    grandchild_pid = int(descendant.stdout.readline())
    print(
        json.dumps(
            {
                "parent": os.getpid(),
                "child": descendant.pid,
                "grandchild": grandchild_pid,
            }
        ),
        flush=True,
    )
    time.sleep(60)


def process_limit() -> None:
    try:
        created = subprocess.run(
            [sys.executable, "-c", "pass"],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"blocked:{type(error).__name__}", flush=True)
        return
    if created.returncode == 0:
        print("escaped", flush=True)
        raise SystemExit(4)
    print(f"blocked:exit-{created.returncode}", flush=True)


parser = argparse.ArgumentParser()
parser.add_argument(
    "mode",
    choices=("tree", "child", "descendant-marker", "process-limit"),
)
parser.add_argument("marker", nargs="?", type=Path)
arguments = parser.parse_args()

if arguments.mode == "process-limit":
    process_limit()
elif arguments.marker is None:
    parser.error("marker is required for this mode")
elif arguments.mode == "tree":
    tree(arguments.marker)
elif arguments.mode == "child":
    child(arguments.marker)
else:
    descendant_marker(arguments.marker)
