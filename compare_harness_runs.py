"""Compare two compatible Harness-Bench runs task by task."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from run_harness_bench import task_score
from view_harness_bench import resolve_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", help="run id or directory")
    parser.add_argument("candidate", help="run id or directory")
    parser.add_argument("--output", help="optional JSON output path")
    options = parser.parse_args(argv)
    report = compare_runs(resolve_run(options.baseline), resolve_run(options.candidate))
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if options.output:
        path = Path(options.output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0


def compare_runs(baseline_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    baseline = _load_run(baseline_dir)
    candidate = _load_run(candidate_dir)
    for field in ("benchmark_commit", "tasks"):
        if baseline["run"].get(field) != candidate["run"].get(field):
            raise ValueError(f"incompatible {field}: {baseline['run'].get(field)!r} != {candidate['run'].get(field)!r}")

    task_ids = list(baseline["run"]["tasks"])
    rows = []
    for task_id in task_ids:
        left = _task_score(baseline_dir, task_id)
        right = _task_score(candidate_dir, task_id)
        delta = round(right - left, 6)
        rows.append({
            "task_id": task_id,
            "baseline": left,
            "candidate": right,
            "delta": delta,
            "result": "win" if delta > 0 else "loss" if delta < 0 else "tie",
        })
    deltas = [row["delta"] for row in rows]
    return {
        "schema_version": 1,
        "benchmark_commit": baseline["run"]["benchmark_commit"],
        "tasks": task_ids,
        "baseline": _identity(baseline_dir, baseline),
        "candidate": _identity(candidate_dir, candidate),
        "candidate_delta": round(statistics.mean(deltas), 6) if deltas else None,
        "wins": sum(row["result"] == "win" for row in rows),
        "ties": sum(row["result"] == "tie" for row in rows),
        "losses": sum(row["result"] == "loss" for row in rows),
        "per_task": rows,
        "note": "Missing, incomplete, and failed scheduled tasks are scored as zero in both runs.",
    }


def _load_run(path: Path) -> dict[str, Any]:
    run = _json(path / "run.json")
    summary = _json(path / "summary.json")
    if not isinstance(run, dict) or not isinstance(summary, dict):
        raise ValueError(f"run metadata or summary missing from {path}")
    return {"run": run, "summary": summary}


def _task_score(run_dir: Path, task_id: str) -> float:
    """One task's contribution, scored by the launcher's own definition.

    Imported rather than reimplemented so a comparison's per-task deltas
    always reconcile with the ``completion_score`` printed beside them.
    """
    return task_score(_json(run_dir / "tasks" / task_id / "status.json"))


def _identity(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    run = data["run"]
    summary = data["summary"]
    return {
        "run_id": run.get("run_id"),
        "path": str(path.resolve()),
        "model": run.get("model"),
        "mode": run.get("mode"),
        "image_id": run.get("image_id"),
        "completion_score": summary.get("completion_score"),
        "llm_usage": summary.get("llm_usage"),
    }


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
