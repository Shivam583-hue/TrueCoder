from __future__ import annotations

import json
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)


def wait_forever() -> None:
    while True:
        time.sleep(1)


read_fd, write_fd = os.pipe()
child_pid = os.fork()
if child_pid == 0:
    os.close(read_fd)
    grandchild_pid = os.fork()
    if grandchild_pid == 0:
        os.close(write_fd)
        wait_forever()
    os.write(write_fd, str(grandchild_pid).encode())
    os.close(write_fd)
    wait_forever()

os.close(write_fd)
grandchild_pid = int(os.read(read_fd, 64))
os.close(read_fd)
print(
    json.dumps(
        {
            "parent": os.getpid(),
            "child": child_pid,
            "grandchild": grandchild_pid,
        }
    ),
    flush=True,
)
wait_forever()
