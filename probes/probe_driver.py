"""Prove the driver against a live container without spending a model call.

The five older probes each prove one half of the *container*. This one proves
the **driver** -- the shared code every benchmark will stand on -- and it does
it with slash commands and a direct ``proc.run``, so it costs nothing and can
run on every change.

Four claims, in the order a real run depends on them:

1. the stream opens and a session can be established (no session, no dialogs)
2. an unsafe Request raises a dialog the manifest approver answers by rule
3. a manifest that does *not* cover the request refuses it, and says why
4. the collectors page a transcript and a ledger back out

    python /opt/sb-driver/driver/../../probes/probe_driver.py
"""

from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver import collect                              # noqa: E402
from driver.approver import Approver, Manifest          # noqa: E402
from driver.turn import establish_session               # noqa: E402
from driver.wire import Client                          # noqa: E402

BASE = os.environ.get("SB_BASE_URL", "http://127.0.0.1:8787")
TOKEN = os.environ.get("SB_HTTP_TOKEN", "benchtoken")

failures = []


def check(label, ok, detail=""):
    print(("   PASS  " if ok else "   FAIL  ") + label
          + (("  -- " + str(detail)) if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def main():
    client = Client(base=BASE, token=TOKEN, thread="probe")

    print("== 1. the wire ==")
    check("server answers", client.wait_until_up(seconds=90))
    client.open_stream()
    check("stream is open", client.stream_state["open"], client.stream_state)

    session = establish_session(client)
    check("session established", session["ok"], session)
    check("ready for a turn", session.get("phase") == "awaiting_input",
          session.get("phase"))
    check("mode is ask, not yolo", session.get("mode") == "ask",
          session.get("mode"))
    print("   bootstrap frames: "
          + str(sorted(client.frames.kinds())))

    print("== 2. a manifest that allows the command ==")
    allowed = Manifest({"proc.run": {"prefixes": ["echo hello"]},
                        "default": "deny"})
    status, answer, decisions = _run_gated(client, allowed,
                                           ["echo", "hello from the box"])
    check("proc.run allowed", status == 200, str(status) + " " + str(answer)[:120])
    check("one decision recorded", len(decisions) == 1, decisions)
    if decisions:
        check("decided allow", decisions[0]["choice"] == "allow",
              decisions[0].get("why"))
        check("matched on detail", decisions[0].get("detail") is not None)

    print("== 3. a manifest that does not ==")
    narrow = Manifest({"proc.run": {"prefixes": ["echo goodbye"]},
                       "default": "deny"})
    status, answer, decisions = _run_gated(client, narrow,
                                           ["echo", "hello from the box"])
    check("proc.run refused", status == 403,
          str(status) + " " + str(answer)[:120])
    check("refusal is coded", str(answer.get("code")) == "approval_declined",
          answer.get("code"))
    if decisions:
        check("decided deny", decisions[0]["choice"] == "deny",
              decisions[0].get("why"))

    print("== 4. the collectors ==")
    cid = collect.conversation_id(client)
    said = collect.transcript(client, cid)
    check("transcript pages", said.get("complete") is True,
          str(len(said.get("messages") or [])) + " rows")
    effects = collect.ledger(client, cid=cid)
    check("ledger reads", effects["status"] == 200,
          str(len(effects["rows"])) + " rows, complete="
          + str(effects["complete"]))
    numbers = collect.metrics(client.frames.snapshot(), [], [],
                              effects["rows"])
    print("   metrics: " + json.dumps({k: numbers[k] for k in
                                       ("tool_calls", "commands",
                                        "ledger_actions")}, default=str)[:300])

    files = collect.workdir("/data")
    check("workdir walks", files.get("count", 0) > 0,
          str(files.get("count")) + " files under /data")

    client.close()
    print("\n== " + ("ALL PASS" if not failures
                     else str(len(failures)) + " FAILED: "
                     + ", ".join(failures)) + " ==")
    return 1 if failures else 0


def _run_gated(client, manifest, argv):
    """Issue an unsafe Request with an approver standing by, and report both.

    The POST blocks while the dialog is open, so it cannot be the caller: the
    approver answers from another thread and the POST then completes with the
    result. That is the whole driver contract in one round trip.
    """
    approver = Approver(client, manifest, log=lambda line: print("   " + line))
    approver.start()
    box = {}

    def call():
        box["status"], box["answer"] = client.post(
            "proc.run", {"argv": argv, "shell": "default"}, timeout=120)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout=120)
    approver.stop()
    return box.get("status", 0), box.get("answer", {}), approver.decisions


if __name__ == "__main__":
    raise SystemExit(main())
