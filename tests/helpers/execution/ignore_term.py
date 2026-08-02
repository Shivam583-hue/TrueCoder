from __future__ import annotations

import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
while True:
    time.sleep(1)
