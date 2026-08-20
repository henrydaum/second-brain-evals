"""Drive one task inside the container and leave a result bundle behind.

    python /opt/sb-driver/driver/run_task.py /work/task.json

One process, one turn, one bundle. The **same command** runs in guest mode --
inside Harbor's or Boundary-Bench's task container, where they install the
agent rather than us -- which is what keeps the four public adapters thin:
an adapter supplies the task and reads the bundle, and never learns anything
about the wire.

Exit codes distinguish the two failures a benchmark must never confuse:

===  ==========================================================
0    the drive completed; whether the agent *succeeded* is in
     ``result.json`` and is the scorer's business, not ours
2    the harness itself failed -- the server never answered, or
     no session could be established, so nothing was measured
===  ==========================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver import collect, turn                        # noqa: E402
from driver.approver import Approver, Manifest          # noqa: E402
from driver.wire import Client                          # noqa: E402

TEMPLATE_MANIFEST = os.environ.get(
    "SB_TEMPLATE_MANIFEST", "/opt/sb-template/template_manifest.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", help="the task spec, as JSON")
    parser.add_argument("--out", default=None,
                        help="bundle directory (default: <workdir>/_result)")
    options = parser.parse_args(argv)

    spec = json.loads(_read(options.spec))
    workdir = spec.get("workdir") or os.path.dirname(
        os.path.abspath(options.spec))
    out = options.out or os.path.join(workdir, "_result")
    os.makedirs(out, exist_ok=True)

    client = Client(base=spec.get("base") or _env("SB_BASE_URL",
                                                  "http://127.0.0.1:8787"),
                    token=spec.get("token") or _env("SB_HTTP_TOKEN", ""),
                    thread=spec.get("thread") or "main",
                    record=os.path.join(out, "frames.jsonl"))

    started = time.time()
    if not client.wait_until_up(seconds=float(spec.get("boot_s", 180))):
        return _bail(out, "server never answered", started)

    # The stream first, and it stays open: attendance follows from it, and an
    # unattended session refuses unsafe Requests without asking anybody.
    client.open_stream()
    session = turn.establish_session(client)
    if not session["ok"]:
        return _bail(out, "no session could be established", started, session)

    baseline = collect.ledger_high_water(client)
    approver = Approver(client, Manifest(spec.get("manifest")),
                        ui=spec.get("ui"), log=_stamped)
    approver.start()

    budget = spec.get("budget") or {}
    _stamped("[task] " + str(spec.get("id")) + " -> " + workdir)
    outcome = turn.run_turn(client, spec["prompt"],
                            wall_s=float(budget.get("wall_s", 900)),
                            stall_s=float(budget.get("stall_s", 300)),
                            approver=approver, log=_stamped)
    approver.stop()

    cid = session.get("conversation_id") or collect.conversation_id(client)
    said = collect.transcript(client, cid)
    effects = collect.ledger(client, cid=cid, since_id=baseline)
    files = collect.workdir(workdir)
    numbers = collect.metrics(client.frames.snapshot(outcome["mark"]),
                              approver.decisions, approver.questions,
                              effects["rows"])
    numbers["llm"] = collect.llm_usage(os.path.join(out, "llm_usage.jsonl"))

    _write(out, "result.json", {
        "task_id": spec.get("id"),
        "outcome": outcome,
        "session": session,
        "metrics": numbers,
        "workdir": workdir,
        "wall_s": round(time.time() - started, 3),
        "stream": client.stream_state,
        "template": _template_manifest(),
        "model": {"model": os.environ.get("SB_LLM_MODEL"),
                  "endpoint": os.environ.get("SB_LLM_ENDPOINT"),
                  "backend": os.environ.get("SB_LLM_BACKEND")},
        "driver_version": 1,
    })
    _write(out, "approvals.json", approver.decisions)
    _write(out, "questions.json", approver.questions)
    _write(out, "transcript.json", said)
    _write(out, "ledger.json", effects)
    _write(out, "files.json", files)

    client.close()
    _stamped("[done] " + outcome["reason"] + " in "
             + str(outcome["elapsed_s"]) + "s, "
             + str(numbers["approvals"]) + " approvals, "
             + str(numbers["tool_calls"]) + " tool calls")
    return 0


def _template_manifest():
    """The provenance a published score has to report beside it."""
    try:
        return json.loads(_read(TEMPLATE_MANIFEST))
    except (OSError, ValueError):
        return None


def _bail(out, why, started, extra=None):
    """A harness failure, recorded in the same place as a real result.

    Written rather than raised because a trial that never ran still has to be
    countable: a suite that silently drops its broken trials reports a mean
    over the runs that happened to work.
    """
    _write(out, "result.json", {"outcome": {"ok": False, "reason": "harness",
                                            "error": why},
                                "detail": extra,
                                "wall_s": round(time.time() - started, 3)})
    _stamped("[harness] " + why)
    return 2


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write(out, name, payload):
    with open(os.path.join(out, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)


def _env(name, fallback):
    return os.environ.get(name) or fallback


def _stamped(line):
    print("%7.1fs %s" % (time.time() % 100000, line), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
