"""Zero-LLM probe for persistent Harness-Bench session modes."""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/sb-driver")

from driver.approver import Approver, Manifest
from driver.turn import establish_session, set_security_mode
from driver.wire import Client


def main() -> int:
    client = Client(token="probe-token", thread="main")
    if not client.wait_until_up(120):
        raise RuntimeError("server did not start")
    client.open_stream()
    approver = Approver(
        client,
        Manifest({"default": "deny"}),
        ui={"policy": "canned", "text": "Proceed."},
        log=None,
    )
    approver.start()
    results = {"session": establish_session(client, require_ask_mode=False)}
    for mode in ("yolo",):
        results.setdefault("modes", []).append(set_security_mode(client, mode))
    approver.stop()
    client.close()

    # Harness-Bench invokes the generic CLI once per round. A new client must
    # reconnect to the same server-side thread without `/new` wiping context.
    client = Client(token="probe-token", thread="main")
    client.wait_until_up(30)
    client.open_stream()
    reused = establish_session(client, require_ask_mode=False, fresh=False)
    results["reused"] = reused
    for mode in ("ask", "lockdown", "ask"):
        results.setdefault("modes", []).append(set_security_mode(client, mode))
    print(json.dumps(results, indent=2))
    client.close()
    if (not results["session"]["ok"] or not reused["ok"]
            or reused.get("mode") != "yolo"
            or not all(item["ok"] for item in results["modes"])):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
