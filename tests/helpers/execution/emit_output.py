from __future__ import annotations

import argparse
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--stdout", default="")
parser.add_argument("--stderr", default="")
parser.add_argument("--exit-code", type=int, default=0)
arguments = parser.parse_args()

sys.stdout.buffer.write(arguments.stdout.encode())
sys.stdout.buffer.flush()
sys.stderr.buffer.write(arguments.stderr.encode())
sys.stderr.buffer.flush()
raise SystemExit(arguments.exit_code)
