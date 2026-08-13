from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_checksums.py RELEASE_DIRECTORY", file=sys.stderr)
        return 2
    directory = Path(sys.argv[1])
    assets = sorted(
        path for path in directory.iterdir() if path.name != "SHA256SUMS"
    )
    if not assets:
        print("the release directory is empty", file=sys.stderr)
        return 1
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in assets
    ]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
