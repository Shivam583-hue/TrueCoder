from __future__ import annotations

import subprocess

children: list[subprocess.Popen] = []
limited = False
try:
    for _ in range(256):
        children.append(subprocess.Popen(["sleep", "30"]))
except OSError:
    limited = True
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait()

print("PID-LIMIT-ENFORCED" if limited else "PID-LIMIT-MISSED")
raise SystemExit(0 if limited else 3)
