from __future__ import annotations

import sys
from pathlib import Path

Path(sys.argv[1]).write_text("started", encoding="utf-8")
