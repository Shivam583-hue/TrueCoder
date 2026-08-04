from __future__ import annotations

import os
import sys
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
POSIX = os.name == "posix"


def skip_module_on_windows(capability: str) -> None:
    if WINDOWS:
        raise unittest.SkipTest(f"{capability} is not available on Windows")


@lru_cache(maxsize=1)
def symlinks_available() -> bool:
    if not WINDOWS:
        return True
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        target.write_text("x", encoding="utf-8")
        try:
            os.symlink(target, root / "link")
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


requires_symlinks = unittest.skipUnless(
    symlinks_available(),
    "creating symbolic links requires elevation on this host",
)

requires_posix_permissions = unittest.skipUnless(
    POSIX,
    "POSIX permission bits are not enforced on this host",
)

requires_posix = unittest.skipUnless(
    POSIX,
    "this behaviour is specific to POSIX hosts",
)
