from __future__ import annotations

import json
import os
import sys

print(json.dumps({name: os.environ.get(name) for name in sys.argv[1:]}))
