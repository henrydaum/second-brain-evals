"""Walk tasks x trials, one fresh container each, and write down everything.

    python -m harness.runner --adapter evals.internal.adapter:Adapter \
        --env-file bench.env --trials 3 --workers 4

An adapter supplies three things and knows nothing about Docker or the wire::

    class Adapter:
        def tasks(self): ...              # id, prompt, manifest, fixtures
        def setup(self, task, dest): ...  # optional: build fixtures
        def score(self, task, bundle): .. # their verifier, or ours

Trials are independent by construction, so they run in parallel. The ceiling
is the model provider's rate limit rather than the host: four containers is a
polite default and the flag exists because the right number is a property of
somebody's API plan, not of this code.

**A trial that fails is still a trial.** Every outcome is written, including
the harness failures, because a suite that silently drops the runs that broke
reports a mean over the runs that happened to work.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import importlib
import json
import os
import statistics
import sys
import time
import traceback

from harness.container import WORKDIR, Trial

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(HERE, "results")


def load_adapter(path):
    """``module:Class`` -- the same shape Harbor's ``--agent-import-path`` uses."""
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr or "Adapter")()


def run_trial(task, trial_index, run_dir, env_file, image, keep=False,
              timeout=3600):
    """One container, one attempt, one bundle on disk."""
    label = str(task["id"]) + "/" + str(trial_index)
    dest = os.path.join(run_dir, str(task["id"]), str(trial_index))
    record = {"task_id": task["id"], "trial": trial_index, "dest": dest}
    started = time.time()
    name = ("sb-" + os.path.basename(run_dir) + "-" + str(task["id"])
            + "-" + str(trial_index)).replace("_", "-").lower()[:60]
    try:
        with Trial(name=name, image=image, env_file=env_file, keep=keep,
                   env={"SB_WRITABLE_DIRS": os.path.dirname(WORKDIR)}) as box:
            box.prepare(_spec(task), fixtures=task.get("fixtures"))
            result = box.drive(timeout=timeout)
            record["exit_code"] = result.returncode
            record["stdout"] = (result.stdout or "")[-4000:]
            record["stderr"] = (result.stderr or "")[-4000:]
            if result.returncode == 2:
                # The driver says it never measured anything. Its own logs are
                # the only account of why, so keep them beside the bundle.
                record["container_logs"] = box.logs()
            box.collect(dest)
    except Exception as e:                                      # noqa: BLE001
        record["error"] = repr(e)
        record["traceback"] = traceback.format_exc()[-2000:]
    record["wall_s"] = round(time.time() - started, 3)
    record["bundle"] = read_bundle(dest)
    print("  " + label + "  " + _verdict(record), flush=True)
    return record


def _spec(task):
    """The task as the in-container driver wants it."""
    return {"id": task["id"], "prompt": task["prompt"],
            "manifest": task.get("manifest") or {},
            "ui": task.get("ui"), "budget": task.get("budget") or {},
            "workdir": WORKDIR}


def read_bundle(dest):
    """The driver's own account of the trial, if it got that far."""
    path = os.path.join(dest, "_result", "result.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _verdict(record):
    bundle = record.get("bundle") or {}
    outcome = bundle.get("outcome") or {}
    if record.get("error"):
        return "HARNESS ERROR  " + str(record["error"])[:90]
    if not bundle:
        return "NO BUNDLE  exit=" + str(record.get("exit_code"))
    metrics = bundle.get("metrics") or {}
    return ("%-13s %5.1fs  approvals=%-3s tools=%-3s"
            % (outcome.get("reason", "?"), outcome.get("elapsed_s", 0),
               metrics.get("approvals", "?"), metrics.get("tool_calls", "?")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", default="evals.internal.adapter:Adapter")
    parser.add_argument("--env-file", default="bench.env")
    parser.add_argument("--image", default=os.environ.get("SB_BENCH_IMAGE",
                                                          "secondbrain:bench"))
    parser.add_argument("--trials", type=int, default=1,
                        help="attempts per task; leaderboards want 3-5")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--task", action="append", default=None,
                        help="only these task ids (repeatable)")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--keep", action="store_true",
                        help="leave containers behind for inspection")
    parser.add_argument("--run-id", default=None)
    options = parser.parse_args(argv)

    if not os.path.exists(options.env_file):
        print("no env file at " + options.env_file
              + " -- it carries SB_LLM_API_KEY and is gitignored",
              file=sys.stderr)
        return 1

    adapter = load_adapter(options.adapter)
    tasks = [t for t in adapter.tasks()
             if not options.task or t["id"] in options.task]
    if not tasks:
        print("no tasks matched", file=sys.stderr)
        return 1

    run_id = options.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(RESULTS, run_id)
    os.makedirs(run_dir, exist_ok=True)
    print("run " + run_id + ": " + str(len(tasks)) + " tasks x "
          + str(options.trials) + " trials on " + options.image, flush=True)

    jobs = [(task, trial) for task in tasks
            for trial in range(1, options.trials + 1)]
    records = []
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(options.workers) as pool:
        futures = [pool.submit(run_trial, task, trial, run_dir,
                               options.env_file, options.image, options.keep,
                               options.timeout)
                   for task, trial in jobs]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())

    for record in records:
        task = next(t for t in tasks if t["id"] == record["task_id"])
        record["score"] = _score(adapter, task, record)

    summary = summarise(records, run_id, time.time() - started)
    with open(os.path.join(run_dir, "summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"summary": summary, "records": records}, handle, indent=2,
                  default=str)
    print_summary(summary)
    return 0


def _score(adapter, task, record):
    """Scoring must never take the run down with it.

    A checker that raises is a bug in the checker, and losing an expensive
    trial's bundle to one is the wrong trade -- the bundle is on disk and can
    be re-scored later without spending another model call.
    """
    scorer = getattr(adapter, "score", None)
    if scorer is None or not record.get("bundle"):
        return None
    try:
        return scorer(task, record["dest"])
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "error": repr(e),
                "traceback": traceback.format_exc()[-1000:]}


def summarise(records, run_id, elapsed):
    by_task = {}
    for record in records:
        by_task.setdefault(record["task_id"], []).append(record)
    rows = []
    for task_id, trials in sorted(by_task.items()):
        scored = [t for t in trials if isinstance(t.get("score"), dict)]
        passes = [t for t in scored if t["score"].get("ok")]
        drives = [t for t in trials if (t.get("bundle") or {})
                  .get("outcome", {}).get("ok")]
        rows.append({
            "task_id": task_id,
            "trials": len(trials),
            "drives_ok": len(drives),
            "passes": len(passes),
            "pass_rate": (len(passes) / len(scored)) if scored else None,
            "approvals": _mean(trials, "approvals"),
            "tool_calls": _mean(trials, "tool_calls"),
            "script_runs": _mean(trials, "script_runs"),
            "questions": _mean(trials, "questions_asked"),
            "elapsed_s": _mean_outcome(trials, "elapsed_s"),
        })
    return {"run_id": run_id, "elapsed_s": round(elapsed, 1),
            "tasks": rows, "trial_count": len(records)}


def _values(trials, key, section="metrics"):
    out = []
    for trial in trials:
        bundle = trial.get("bundle") or {}
        value = (bundle.get(section) or {}).get(key)
        if isinstance(value, (int, float)):
            out.append(value)
    return out


def _mean(trials, key):
    values = _values(trials, key)
    return round(statistics.mean(values), 2) if values else None


def _mean_outcome(trials, key):
    values = _values(trials, key, section="outcome")
    return round(statistics.mean(values), 1) if values else None


def print_summary(summary):
    print("\n" + "=" * 78)
    print("%-24s %6s %7s %7s %9s %7s %7s"
          % ("task", "trials", "drives", "passes", "approvals", "tools",
             "secs"))
    print("-" * 78)
    for row in summary["tasks"]:
        print("%-24s %6s %7s %7s %9s %7s %7s"
              % (row["task_id"][:24], row["trials"], row["drives_ok"],
                 row["passes"], row["approvals"], row["tool_calls"],
                 row["elapsed_s"]))
    print("=" * 78)
    print("run " + summary["run_id"] + " in " + str(summary["elapsed_s"])
          + "s\n")


if __name__ == "__main__":
    raise SystemExit(main())
