"""Drop the generic adapter to the unprivileged Second Brain identity.

Harness-Bench's runner needs its task definitions and oracle, but the evaluated
agent must never be able to inspect either. The runner invokes this shim as
root; it grants UID 1000 only the generated sandbox/workspace, then permanently
drops privileges before starting the ordinary driver.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


APP_UID = 1000
APP_GID = 1000


def _value(flag: str) -> Path:
    index = sys.argv.index(flag)
    return Path(sys.argv[index + 1]).resolve()


def _grant(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        os.chown(root, APP_UID, APP_GID)
        for name in dirs:
            os.chown(os.path.join(root, name), APP_UID, APP_GID)
        for name in files:
            os.chown(os.path.join(root, name), APP_UID, APP_GID)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("isolated driver must be launched by the root scorer")
    sandbox = _value("--sandbox")
    _grant(sandbox)
    os.setgroups([])
    os.setgid(APP_GID)
    os.setuid(APP_UID)
    os.execv(sys.executable, [sys.executable, "/opt/sb-evals/drive_round.py", *sys.argv[1:]])


if __name__ == "__main__":
    main()
