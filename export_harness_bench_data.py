"""Export Harness-Bench runs into normalized, analysis-ready datasets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results" / "harness-bench"
INPUT_USD_PER_MILLION = {"minimax/MiniMax-M3": 0.30}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_sqlite(path: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
    """Write normalized tables with conservative SQLite scalar types."""
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        for name, rows in tables.items():
            fields = list(dict.fromkeys(key for row in rows for key in row))
            if not fields:
                continue
            types = {}
            for field in fields:
                values = [row.get(field) for row in rows if row.get(field) is not None]
                types[field] = ("INTEGER" if values and all(isinstance(value, (bool, int)) for value in values)
                                else "REAL" if values and all(isinstance(value, (bool, int, float)) for value in values)
                                else "TEXT")
            columns = ", ".join(f'"{field}" {types[field]}' for field in fields)
            connection.execute(f'CREATE TABLE "{name}" ({columns})')
            placeholders = ",".join("?" for _ in fields)
            quoted = ",".join(f'"{field}"' for field in fields)
            values = []
            for row in rows:
                record = []
                for field in fields:
                    value = row.get(field)
                    if isinstance(value, bool):
                        value = int(value)
                    elif isinstance(value, (dict, list)):
                        value = json_cell(value)
                    record.append(value)
                values.append(record)
            connection.executemany(
                f'INSERT INTO "{name}" ({quoted}) VALUES ({placeholders})', values)
        for table in ("oracle_checks", "model_calls", "tool_calls", "approvals", "events"):
            if tables.get(table):
                connection.execute(f'CREATE INDEX "idx_{table}_trial" ON "{table}" (trial_id)')
        connection.execute("""
            CREATE VIEW trial_efficiency AS
            SELECT trial_id, outcome_score, elapsed_sec, model_calls,
                   prompt_tokens_known, estimated_input_cost_usd, tool_calls,
                   tool_errors, repeated_exact_actions, validate_calls, run_script_calls
            FROM trials
        """)
        connection.execute("""
            CREATE VIEW failed_checks AS
            SELECT c.*, t.task_id, t.model, t.mode, t.difficulty
            FROM oracle_checks c JOIN trials t USING (trial_id)
            WHERE c."pass" = 0
        """)
        connection.commit()
    finally:
        connection.close()


def official_result(task_dir: Path, status: dict[str, Any]) -> dict[str, Any]:
    relative = status.get("official_result")
    return read_json(task_dir / relative) if relative else {}


def export_runs(run_dirs: list[Path], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    trials: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    raw_events: list[dict[str, Any]] = []

    for run_dir in run_dirs:
        run = read_json(run_dir / "run.json") or {}
        summary = read_json(run_dir / "summary.json") or {}
        by_task = {row.get("task_id"): row for row in summary.get("tasks") or []}
        metadata = run.get("task_metadata") or {}
        for task_id in run.get("tasks") or []:
            task_dir = run_dir / "tasks" / task_id
            status = read_json(task_dir / "status.json") or by_task.get(task_id) or {}
            official = official_result(task_dir, status)
            oracle = official.get("oracle_result") or {}
            trial_id = f"{run_dir.name}/{task_id}"
            model_rows: list[dict[str, Any]] = []
            tool_by_id: dict[str, dict[str, Any]] = {}
            action_counts: dict[str, int] = {}
            repeat_count = 0
            tool_errors = 0
            assistant_chars = 0

            for event_index, event in enumerate(read_jsonl(task_dir / "events.jsonl")):
                frame = event.get("frame") or {}
                raw_events.append({
                    "trial_id": trial_id, "event_index": event_index, "at": event.get("at"),
                    "source": event.get("source"), "kind": event.get("kind"),
                    "frame_kind": frame.get("kind"), "event_json": json_cell(event),
                })
                if event.get("source") == "llm" and event.get("kind") == "llm_call":
                    payload = event.get("payload") or {}
                    row = {
                        "trial_id": trial_id, "call_index": len(model_rows) + 1,
                        "at": event.get("at"), "model": payload.get("model"),
                        "ok": payload.get("ok"), "duration_s": payload.get("duration_s"),
                        "prompt_tokens": payload.get("prompt_tokens"),
                        "has_tool_calls": payload.get("has_tool_calls"),
                        "error": payload.get("error"),
                    }
                    model_rows.append(row)
                    calls.append(row)
                    continue
                if event.get("source") == "approver":
                    payload = event.get("payload") or {}
                    approvals.append({
                        "trial_id": trial_id, "at": event.get("at"),
                        "kind": event.get("kind"), "choice": payload.get("choice"),
                        "type": payload.get("type"), "subject": payload.get("subject"),
                        "latency_s": payload.get("latency_s"), "payload_json": json_cell(payload),
                    })
                payload = frame.get("payload")
                if frame.get("kind") == "stream_delta" and isinstance(payload, dict):
                    assistant_chars += len(str(payload.get("delta") or ""))
                if frame.get("kind") != "tool_status" or not isinstance(payload, dict):
                    continue
                call_id = str(payload.get("call_id") or f"event-{event_index}")
                name = payload.get("tool_name") or payload.get("command_name") or payload.get("kind") or "tool"
                if payload.get("status") == "started":
                    args = payload.get("args") or {}
                    signature = hashlib.sha256((str(name) + "\n" + json_cell(args)).encode()).hexdigest()[:16]
                    seen = action_counts.get(signature, 0)
                    repeat_count += int(seen > 0)
                    action_counts[signature] = seen + 1
                    tool_by_id[call_id] = {
                        "trial_id": trial_id, "call_id": call_id, "tool_name": name,
                        "started_at": event.get("at"), "finished_at": None,
                        "duration_s": None, "ok": None, "error": None,
                        "is_repeated_exact_action": bool(seen), "args_json": json_cell(args),
                        "summary": None,
                    }
                elif payload.get("status") == "finished":
                    row = tool_by_id.setdefault(call_id, {
                        "trial_id": trial_id, "call_id": call_id, "tool_name": name,
                        "started_at": None, "is_repeated_exact_action": False, "args_json": "{}",
                    })
                    row.update({"finished_at": event.get("at"), "ok": payload.get("ok"),
                                "error": payload.get("error"), "summary": payload.get("summary")})
                    if row.get("started_at") is not None and event.get("at") is not None:
                        row["duration_s"] = event["at"] - row["started_at"]
                    tool_errors += int(payload.get("ok") is False)
            tools.extend(tool_by_id.values())

            prompt_values = [row["prompt_tokens"] for row in model_rows
                             if isinstance(row.get("prompt_tokens"), (int, float))]
            integrity_checks = [item for item in oracle.get("checks") or []
                                if item.get("id") in {"fixture_integrity", "fixtures_unchanged",
                                                     "input_integrity", "code_integrity"}]
            integrity = "n/a" if not integrity_checks else (
                "pass" if all(item.get("pass") for item in integrity_checks) else "fail")
            validity_flags = []
            if status.get("state") != "complete":
                validity_flags.append(str(status.get("state") or "unknown_state"))
            if status.get("adapter_ok") is False:
                validity_flags.append("adapter_failed")
            if integrity == "fail":
                validity_flags.append("integrity_check_failed")
            model = status.get("model") or run.get("model")
            prompt_total = sum(prompt_values) if prompt_values else None
            trials.append({
                "trial_id": trial_id, "run_id": run_dir.name, "task_id": task_id,
                "title": (metadata.get(task_id) or {}).get("title"),
                "task_class": (metadata.get(task_id) or {}).get("class"),
                "difficulty": (metadata.get(task_id) or {}).get("difficulty"),
                "mode": status.get("mode") or run.get("mode"), "model": model,
                "kernel_commit": (run.get("template") or {}).get("kernel_commit"),
                "benchmark_commit": run.get("benchmark_commit"), "state": status.get("state"),
                "outcome_score": status.get("outcome_score"), "combined_score": status.get("combined_score"),
                "elapsed_sec": status.get("elapsed_sec"), "model_calls": len(model_rows),
                "prompt_tokens_known": prompt_total,
                "prompt_tokens_first_call": prompt_values[0] if prompt_values else None,
                "prompt_tokens_last_call": prompt_values[-1] if prompt_values else None,
                "prompt_tokens_max_call": max(prompt_values) if prompt_values else None,
                "estimated_input_cost_usd": (prompt_total * INPUT_USD_PER_MILLION[model] / 1_000_000)
                if prompt_total is not None and model in INPUT_USD_PER_MILLION else None,
                "completion_tokens": None, "tool_calls": len(tool_by_id), "tool_errors": tool_errors,
                "repeated_exact_actions": repeat_count, "assistant_stream_chars": assistant_chars,
                "run_script_calls": sum(row.get("tool_name") == "run_script" for row in tool_by_id.values()),
                "validate_calls": sum(row.get("tool_name") == "validate" for row in tool_by_id.values()),
                "input_integrity": integrity, "validity_flags": ";".join(validity_flags),
                "reliability_trials": 1, "reliability_estimate": None,
                "events_path": str((task_dir / "events.jsonl").resolve()),
                "official_result_path": str((task_dir / status["official_result"]).resolve())
                if status.get("official_result") else None,
            })
            for item in oracle.get("checks") or []:
                checks.append({
                    "trial_id": trial_id, "check_id": item.get("id"), "pass": item.get("pass"),
                    "weight": item.get("weight"), "detail_json": json_cell(item.get("detail")),
                })

    write_csv(output / "trials.csv", trials)
    write_csv(output / "oracle_checks.csv", checks)
    write_csv(output / "model_calls.csv", calls)
    write_csv(output / "tool_calls.csv", tools)
    write_csv(output / "approvals.csv", approvals)
    with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
        for row in raw_events:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tables = {
        "trials": trials, "oracle_checks": checks, "model_calls": calls,
        "tool_calls": tools, "approvals": approvals, "events": raw_events,
    }
    write_sqlite(output / "harness_bench.sqlite", tables)
    manifest = {
        "schema_version": 1, "generated_at": time.time(),
        "run_count": len(run_dirs), "trial_count": len(trials),
        "tables": {name: len(rows) for name, rows in tables.items()},
        "primary_dataset": "harness_bench.sqlite",
        "pricing_assumptions_usd_per_million_input_tokens": INPUT_USD_PER_MILLION,
        "notes": ["Completion-token usage is unavailable from current Second Brain telemetry.",
                  "Reliability requires repeated trials and is intentionally not inferred."],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", help="run id/path; repeat, or omit for every run")
    parser.add_argument("--output", default=str(RESULTS / "analysis" / "latest"))
    options = parser.parse_args(argv)
    if options.run:
        run_dirs = [Path(value).resolve() if Path(value).exists() else RESULTS / value for value in options.run]
    else:
        run_dirs = sorted(path for path in RESULTS.iterdir()
                          if path.is_dir() and (path / "run.json").exists())
    run_dirs = [path for path in run_dirs if (path / "run.json").exists()]
    if not run_dirs:
        parser.error("no Harness-Bench runs found")
    output = Path(options.output).resolve()
    manifest = export_runs(run_dirs, output)
    print(json.dumps({"output": str(output), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
