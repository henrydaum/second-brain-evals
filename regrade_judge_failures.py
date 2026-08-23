"""Re-grade the trials whose LLM judge failed, using a structurally-trimmed payload.

**Why this exists.** Upstream truncates the judge payload with a raw string
slice::

    payload = json.dumps(trace)[:24000] + "...[truncated]"

which cuts mid-string *inside* the JSON. The judge is handed a blob that ends
mid-token, and instead of emitting rubric JSON it continues the transcript. The
verdict then either fails to parse (``parse_error``) or parses into some stray
object with no ``scores`` key -- the silent case, where ``skipped`` and
``parse_error`` are both false and nothing marks the trial as ungraded.

Either way ``process_effective`` and ``security`` both fall back to **1.0**, so
``combined`` collapses to the raw outcome score. A broken judge is scored as a
flawless run.

**The fix.** Trim the trace *data* to fit the budget, then serialize -- never
slice the serialized output. Truncating a string *value* and re-dumping is
always valid JSON; slicing the dump is not. The character budget is unchanged
at upstream's 24000, so the judge sees the same volume of information, just
well-formed.

Reads the read-only snapshot and writes to ``derived/regraded/``; it never
touches the original runs.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BENCH = ROOT / "build" / "harness-bench-src"
SNAPSHOT = ROOT / "results" / "study-2026-08" / "raw-snapshot"
OUTDIR = ROOT / "results" / "study-2026-08" / "derived" / "regraded"
BUDGET = 24000

sys.path.insert(0, str(BENCH / "src"))


def load_env(path: Path) -> dict[str, str]:
    """Read bench.env without exporting anything we were not asked for."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cap_strings(obj: Any, cap: int) -> Any:
    """Cap every string *value* in the structure. Result is still valid JSON."""
    if isinstance(obj, str):
        return obj if len(obj) <= cap else obj[:cap] + f"...[+{len(obj) - cap} chars]"
    if isinstance(obj, list):
        return [cap_strings(v, cap) for v in obj]
    if isinstance(obj, dict):
        return {k: cap_strings(v, cap) for k, v in obj.items()}
    return obj


def drop_middle(items: list, keep_head: int, keep_tail: int) -> list:
    if len(items) <= keep_head + keep_tail:
        return items
    elided = len(items) - keep_head - keep_tail
    marker = {"role": "system",
              "content": f"...[{elided} messages elided to fit the rubric budget]"}
    return items[:keep_head] + [marker] + items[-keep_tail:]


def size(trace: dict) -> int:
    return len(json.dumps(trace, ensure_ascii=False))


def trim_trace(trace: dict, budget: int = BUDGET) -> tuple[dict, str]:
    """Shrink the trace until it serializes under ``budget``. Returns (trace, note)."""
    if size(trace) <= budget:
        return trace, "untrimmed"

    for cap in (8000, 4000, 2000, 1000, 500, 250, 120, 60):
        cand = cap_strings(copy.deepcopy(trace), cap)
        if size(cand) <= budget:
            return cand, f"string-cap={cap}"

    # Still too large: the transcript has too many entries, not merely long ones.
    cand = cap_strings(copy.deepcopy(trace), 60)
    for head, tail in ((40, 40), (25, 25), (15, 15), (8, 8), (4, 4), (2, 2)):
        probe = copy.deepcopy(cand)
        if isinstance(probe.get("unified_transcript"), list):
            probe["unified_transcript"] = drop_middle(probe["unified_transcript"], head, tail)
        if isinstance(probe.get("rounds"), list):
            probe["rounds"] = drop_middle(probe["rounds"], head, tail)
        if size(probe) <= budget:
            return probe, f"string-cap=60,keep={head}+{tail}"

    probe = copy.deepcopy(cand)
    probe["rounds"] = []
    probe["unified_transcript"] = drop_middle(probe.get("unified_transcript") or [], 2, 2)
    return probe, "string-cap=60,rounds-dropped"


def find_official(task_dir: Path) -> Path | None:
    hits = sorted(task_dir.glob("official-results/*/*/*.json"))
    return hits[0] if hits else None


def find_sandbox(task_dir: Path) -> Path | None:
    for d in sorted(task_dir.glob("sandboxes/*/*/oc-bench-v2-*")):
        if (d / "usage-proxy").is_dir():
            return d
    return None


def failure_kind(rubric: dict) -> str:
    if rubric.get("skipped"):
        return "skipped"
    if rubric.get("parse_error"):
        return "parse_error"
    return "silent"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--out", default=str(OUTDIR))
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--run", action="append", help="limit to run id; repeatable")
    ap.add_argument("--limit", type=int, help="stop after N trials")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be re-graded and the payload sizes, with no API call")
    opts = ap.parse_args(argv)

    snap = Path(opts.snapshot).resolve()
    out = Path(opts.out).resolve()

    from harnessbench.grading import process_grade
    from harnessbench.tasks import load_tasks

    tasks = load_tasks(BENCH / "tasks")

    # The per-task rubric resolves off TaskSpec.task_dir, but the *default*
    # rubric resolves off project_root, which upstream computes as parents[2]
    # -- correct inside the container, wrong for this checkout.
    process_grade.resolve_project_root = lambda: BENCH

    real_extract = process_grade.extract_proxy_trace_incremental
    seen: dict[str, dict[str, Any]] = {}

    def patched_extract(proxy_dir: Path) -> dict:
        trace = real_extract(proxy_dir)
        if trace.get("error"):
            return trace
        before = size(trace)
        trimmed, note = trim_trace(trace, opts.budget)
        seen[str(proxy_dir)] = {"before": before, "after": size(trimmed), "trim": note}
        return trimmed

    process_grade.extract_proxy_trace_incremental = patched_extract

    if not opts.dry_run:
        env = load_env(ROOT / "bench.env")
        key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print("ANTHROPIC_API_KEY not found in bench.env or environment", file=sys.stderr)
            return 2
        os.environ["RUBRIC_API_KEY"] = key
        os.environ["RUBRIC_BASE_URL"] = "https://api.anthropic.com/v1"
        os.environ["RUBRIC_MODEL"] = "claude-sonnet-4-6"
    os.environ.pop("HARNESSBENCH_SKIP_PROCESS_GRADE", None)

    run_dirs = sorted(d for d in snap.iterdir() if d.is_dir())
    if opts.run:
        run_dirs = [d for d in run_dirs if d.name in set(opts.run)]

    todo: list[tuple[Path, Path, dict]] = []
    for rd in run_dirs:
        for td in sorted((rd / "tasks").iterdir()):
            if not td.is_dir():
                continue
            of = find_official(td)
            if of is None:
                continue
            blob = json.loads(of.read_text(encoding="utf-8", errors="replace"))
            sc = blob.get("scoring") or {}
            if sc.get("process_score") is not None:
                continue
            todo.append((rd, td, blob))

    print(f"{len(todo)} trials need re-grading across {len(run_dirs)} runs")
    if opts.limit:
        todo = todo[: opts.limit]

    out.mkdir(parents=True, exist_ok=True)
    results = []
    for i, (rd, td, blob) in enumerate(todo, 1):
        task_id = td.name
        sandbox = find_sandbox(td)
        task = tasks.get(task_id)
        old = blob.get("scoring") or {}
        row: dict[str, Any] = {
            "run_id": rd.name,
            "task_id": task_id,
            "old_process_score": old.get("process_score"),
            "old_combined_score": old.get("combined_score"),
            "old_outcome_score": old.get("outcome_score"),
            "old_failure_kind": failure_kind(old.get("rubric") or {}),
        }
        label = f"[{i}/{len(todo)}] {rd.name[9:30]:22} {task_id:45}"

        if sandbox is None or task is None:
            row["error"] = "no sandbox" if sandbox is None else "no task spec"
            results.append(row)
            print(f"{label} SKIP {row['error']}")
            continue

        if opts.dry_run:
            patched_extract(sandbox / "usage-proxy")
            info = seen.get(str(sandbox / "usage-proxy"), {})
            row.update({"payload_before": info.get("before"),
                        "payload_after": info.get("after"),
                        "trim": info.get("trim")})
            results.append(row)
            print(f"{label} {row['payload_before'] or 0:>9,} -> "
                  f"{row['payload_after'] or 0:>6,}  {row['trim']}")
            continue

        try:
            scoring = process_grade.compute_scoring(task, sandbox, blob.get("oracle_result") or {})
        except Exception as e:  # noqa: BLE001 - one bad trial must not kill the batch
            row["error"] = f"{type(e).__name__}: {e}"
            results.append(row)
            print(f"{label} ERROR {row['error']}")
            continue

        info = seen.get(str(sandbox / "usage-proxy"), {})
        rubric = scoring.get("rubric") or {}
        scores = rubric.get("scores") or {}
        row.update({
            "new_process_score": scoring.get("process_score"),
            "new_security_score": scoring.get("security_score"),
            "new_combined_score": scoring.get("combined_score"),
            "new_outcome_score": scoring.get("outcome_score"),
            "judge_tool_use": scores.get("tool_use_appropriate"),
            "judge_consistency": scores.get("consistency"),
            "judge_robustness": scores.get("robustness"),
            "rubric_prompt_source": scoring.get("rubric_prompt_source"),
            "still_failed": scoring.get("process_score") is None,
            "trim": info.get("trim"),
            "payload_before": info.get("before"),
        })
        if row["still_failed"]:
            row["fail_reason"] = rubric.get("reason") or (
                "parse_error" if rubric.get("parse_error") else "empty scores")

        (out / f"{rd.name}__{task_id}.json").write_text(
            json.dumps({"run_id": rd.name, "task_id": task_id, "scoring": scoring},
                       indent=2, ensure_ascii=False),
            encoding="utf-8")
        results.append(row)
        flag = "STILL FAILED" if row["still_failed"] else "ok"
        print(f"{label} process {row['old_process_score']} -> {row['new_process_score']}  "
              f"combined {row['old_combined_score']} -> {row['new_combined_score']}  {flag}")

    summary = out / ("dry-run.json" if opts.dry_run else "regrade_summary.json")
    summary.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
