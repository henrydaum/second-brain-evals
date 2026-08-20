"""Turn Harness-Bench run directories into one analysis-ready SQLite database.

**Run directories are the source of truth; this database is derived.** Every
export rebuilds the rows for the runs it touches, so re-exporting can never
double-count and a schema change costs nothing but a re-run. That is the whole
reason the accumulating store is safe to point at a corpus assembled over many
sessions -- which is how the 106-task suite actually gets run, a piece at a
time as usage allows.

Unchanged runs are skipped by fingerprint (``source_runs``), so refreshing a
large corpus stays cheap while still being a full rebuild of anything that
moved.

    python export_harness_bench_data.py                 # every run on disk
    python export_harness_bench_data.py --run easy-3    # just this one
    python export_harness_bench_data.py --rebuild       # ignore fingerprints

Two properties the schema exists to protect:

* **Unknown is never zero.** A token count or a price the provider did not
  supply lands as ``NULL``. Summing a missing count as zero understates cost
  while every number still looks plausible.
* **Configuration travels with the measurement.** ``kernel_commit``,
  ``profile``, ``mode`` and ``model`` are on every trial, so results from
  different builds or plugin sets never silently average together.
"""

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
DATABASE_NAME = "harness_bench.sqlite"
#: Prices live beside the model description so a published score can be
#: reproduced from committed files. Loaded lazily so the exporter still works
#: in a checkout that has not written one yet.
MODELS_FILE = ROOT / "models.json"

#: Explicit column types per table.
#:
#: Declared rather than inferred from whatever rows happen to be present,
#: because incremental refresh demands a stable schema: two runs that differ
#: in which optional fields they carry must still land in the same columns, or
#: a later export silently reshapes the table under existing queries.
SCHEMA: dict[str, dict[str, str]] = {
    "jobs": {
        "job_id": "TEXT PRIMARY KEY", "state": "TEXT", "model": "TEXT",
        "profile": "TEXT", "mode": "TEXT", "repeats": "INTEGER",
        "task_count": "INTEGER", "trial_count": "INTEGER",
        "selector_json": "TEXT", "notes": "TEXT", "created_at": "REAL",
        "updated_at": "REAL", "paused_reason": "TEXT",
    },
    "runs": {
        "run_id": "TEXT PRIMARY KEY", "job_id": "TEXT", "replicate": "INTEGER",
        "created_at": "REAL", "mode": "TEXT", "model": "TEXT", "profile": "TEXT",
        "tool_profile": "TEXT", "visible_tools_json": "TEXT",
        "kernel_commit": "TEXT", "store_commit": "TEXT",
        "benchmark_commit": "TEXT", "image": "TEXT", "image_id": "TEXT",
        "task_count": "INTEGER", "completion_score": "REAL",
    },
    "trials": {
        "trial_id": "TEXT PRIMARY KEY", "run_id": "TEXT", "job_id": "TEXT",
        "replicate": "INTEGER", "task_id": "TEXT", "title": "TEXT",
        "task_class": "TEXT", "difficulty": "TEXT", "rounds": "INTEGER",
        "mode": "TEXT", "model": "TEXT", "profile": "TEXT",
        "kernel_commit": "TEXT", "store_commit": "TEXT",
        "benchmark_commit": "TEXT", "state": "TEXT",
        "outcome_score": "REAL", "score": "REAL", "combined_score": "REAL",
        "adapter_ok": "INTEGER",
        "elapsed_sec": "REAL", "model_time_sec": "REAL", "model_calls": "INTEGER",
        # Sum of each call's whole prompt: what the provider bills, NOT the
        # context size. ``input_tokens_largest_call`` answers that instead.
        "input_tokens_billed": "INTEGER",
        "input_tokens_largest_call": "INTEGER",
        # The discounted share OF the billed input, never an addition to it.
        "cached_input_tokens": "INTEGER",
        "input_tokens_uncached": "INTEGER",
        "output_tokens": "INTEGER",
        "input_complete": "INTEGER", "output_complete": "INTEGER",
        "tokens_complete": "INTEGER",
        "cost_input_usd": "REAL", "cost_output_usd": "REAL",
        "cost_total_usd": "REAL", "pricing_version": "TEXT",
        "tool_calls": "INTEGER", "tool_errors": "INTEGER",
        "repeated_exact_actions": "INTEGER", "assistant_stream_chars": "INTEGER",
        "run_script_calls": "INTEGER", "validate_calls": "INTEGER",
        "shell_calls": "INTEGER",
        "approvals": "INTEGER", "approvals_denied": "INTEGER",
        "questions_asked": "INTEGER",
        "input_integrity": "TEXT", "validity_flags": "TEXT",
        "message_count": "INTEGER", "round_count": "INTEGER",
        "tool_names_json": "TEXT",
        "transcript_path": "TEXT", "events_path": "TEXT",
        "official_result_path": "TEXT",
    },
    "oracle_checks": {
        "trial_id": "TEXT", "check_id": "TEXT", "pass": "INTEGER",
        "weight": "REAL", "detail_json": "TEXT",
    },
    "model_calls": {
        "trial_id": "TEXT", "call_index": "INTEGER", "at": "REAL",
        "model": "TEXT", "ok": "INTEGER", "duration_s": "REAL",
        "prompt_tokens": "INTEGER", "cached_prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER", "has_tool_calls": "INTEGER",
        "error": "TEXT",
    },
    "tool_calls": {
        "trial_id": "TEXT", "call_id": "TEXT", "tool_name": "TEXT",
        "started_at": "REAL", "finished_at": "REAL", "duration_s": "REAL",
        "ok": "INTEGER", "error": "TEXT", "is_repeated_exact_action": "INTEGER",
        "args_json": "TEXT", "summary": "TEXT",
    },
    "approvals": {
        "trial_id": "TEXT", "at": "REAL", "kind": "TEXT", "choice": "TEXT",
        "type": "TEXT", "subject": "TEXT", "latency_s": "REAL",
        "payload_json": "TEXT",
    },
    "messages": {
        "trial_id": "TEXT", "round": "INTEGER", "message_index": "INTEGER",
        "role": "TEXT", "content": "TEXT", "tool_calls_json": "TEXT",
        "tool_name": "TEXT", "content_chars": "INTEGER",
    },
    "driver_rounds": {
        "trial_id": "TEXT", "round": "INTEGER", "ok": "INTEGER",
        "reason": "TEXT", "wall_s": "REAL", "security_mode": "TEXT",
        "granted_mode": "TEXT", "tool_calls": "INTEGER", "script_runs": "INTEGER",
        "shell_runs": "INTEGER", "approvals": "INTEGER",
        "approvals_denied": "INTEGER", "questions_asked": "INTEGER",
        "ledger_failed": "INTEGER", "ledger_refused": "INTEGER",
        "stream_deltas": "INTEGER", "errors": "INTEGER",
        "tools_json": "TEXT", "approvals_by_type_json": "TEXT",
        "final_text": "TEXT",
    },
    "events": {
        "trial_id": "TEXT", "event_index": "INTEGER", "at": "REAL",
        "source": "TEXT", "kind": "TEXT", "frame_kind": "TEXT",
        "event_json": "TEXT",
    },
    "source_runs": {
        "run_id": "TEXT PRIMARY KEY", "fingerprint": "TEXT",
        "exported_at": "REAL", "run_path": "TEXT",
    },
}

#: Tables whose rows belong to a run and are therefore replaced wholesale when
#: that run changes. ``jobs`` is rebuilt from the job files every time.
RUN_SCOPED = ("trials", "oracle_checks", "model_calls", "tool_calls",
              "approvals", "messages", "driver_rounds", "events")

VIEWS = {
    # What a repeated task actually tells you: the spread, not just the mean.
    # Grouped by the full configuration because a score is only comparable
    # against another score from the same build and plugin set.
    "task_reliability": """
        SELECT model, profile, mode, kernel_commit, task_id, difficulty,
               COUNT(*) AS trials,
               ROUND(AVG(score), 4) AS mean_score,
               MIN(score) AS min_score, MAX(score) AS max_score,
               ROUND(AVG(CASE WHEN score >= 1.0 THEN 1.0 ELSE 0.0 END), 4) AS pass_rate,
               ROUND(AVG(cost_total_usd), 6) AS mean_cost_usd,
               ROUND(AVG(elapsed_sec), 1) AS mean_elapsed_sec
        FROM trials
        GROUP BY model, profile, mode, kernel_commit, task_id, difficulty
    """,
    # The headline number. Averaging per task FIRST is load-bearing: a flat
    # mean over trials lets a task that happened to be repeated three times
    # outweigh one that was run once, so the score would drift with the
    # scheduling history rather than with the harness.
    "config_scores": """
        SELECT model, profile, mode, kernel_commit,
               COUNT(*) AS tasks,
               SUM(trials) AS trials,
               ROUND(AVG(mean_score), 4) AS completion_score,
               ROUND(AVG(pass_rate), 4) AS pass_rate,
               -- Cost of ONE pass over the task set at this configuration:
               -- the per-task mean summed across tasks, not a total over
               -- however many replicates happened to run.
               ROUND(SUM(mean_cost_usd), 4) AS cost_usd_per_sweep,
               ROUND(AVG(mean_elapsed_sec), 1) AS mean_elapsed_sec
        FROM task_reliability
        GROUP BY model, profile, mode, kernel_commit
    """,
    "trial_efficiency": """
        SELECT trial_id, task_id, difficulty, profile, mode, score, elapsed_sec,
               model_calls, input_tokens_billed, cached_input_tokens,
               output_tokens, cost_total_usd, tool_calls, tool_errors,
               repeated_exact_actions, validate_calls, run_script_calls
        FROM trials
    """,
    "failed_checks": """
        SELECT c.*, t.task_id, t.model, t.mode, t.profile, t.difficulty
        FROM oracle_checks c JOIN trials t USING (trial_id)
        WHERE c."pass" = 0
    """,
}


# -- small helpers ----------------------------------------------------

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


def number(value: Any) -> int | float | None:
    """A real number, or nothing. ``bool`` is not a token count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def pricing_table() -> tuple[dict[str, Any], str]:
    data = read_json(MODELS_FILE) or {}
    return data.get("models") or {}, str(data.get("pricing_version") or "unknown")


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def fingerprint(run_dir: Path) -> str:
    """Cheap "has anything moved" signature for one run directory.

    Size and mtime of the files an export reads, rather than their contents: a
    full hash of every event log would cost more than the re-export it saves.
    """
    parts = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(f"{path.relative_to(run_dir)}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


# -- cost -------------------------------------------------------------

def costs(model: str, billed: int | None, cached: int | None, output: int | None,
          prices: dict[str, Any]) -> dict[str, Any]:
    """Money, or ``None`` where a price or a count is missing.

    ``cached`` is a share of ``billed``, so the input total is *split* between
    the cached and uncached rates rather than having a cached charge added to
    it. When the provider reported no cached figure the whole input is billed
    at the full rate: that is the number you would compute without cache
    information at all, and it is an upper bound rather than a guess.
    """
    pricing = (prices.get(model) or {}).get("pricing") or {}
    input_rate = number(pricing.get("input_usd_per_mtok"))
    output_rate = number(pricing.get("output_usd_per_mtok"))
    cached_rate = number(pricing.get("cached_input_usd_per_mtok"))

    cost_input = None
    if billed is not None and input_rate is not None:
        hit = cached or 0
        miss = max(billed - hit, 0)
        rate_for_hits = input_rate if cached_rate is None else cached_rate
        cost_input = (miss * input_rate + hit * rate_for_hits) / 1_000_000

    cost_output = None
    if output is not None and output_rate is not None:
        cost_output = output * output_rate / 1_000_000

    # A partial total would be read as a total. Better to say nothing.
    total = (round(cost_input + cost_output, 6)
             if cost_input is not None and cost_output is not None else None)
    return {
        "cost_input_usd": round(cost_input, 6) if cost_input is not None else None,
        "cost_output_usd": round(cost_output, 6) if cost_output is not None else None,
        "cost_total_usd": total,
    }


# -- reading one trial ------------------------------------------------

def driver_bundles(task_dir: Path) -> list[tuple[int, Path]]:
    """Every round's driver bundle, in order.

    A multi-round task drives the same conversation more than once and leaves
    one bundle per round; taking only the first would lose most of a
    long-running-autonomy task.
    """
    found = []
    for path in sorted((task_dir / "sandboxes").rglob("round-*/result.json")):
        name = path.parent.name.rsplit("-", 1)[-1]
        found.append((int(name) if name.isdigit() else len(found) + 1, path.parent))
    return found


def messages_from(bundle: Path, trial_id: str, round_number: int) -> list[dict[str, Any]]:
    """Index one round's transcript.

    Assistant turns are stored by the kernel as a JSON blob in ``content``
    holding both the text and the tool calls. Unpacking it here is what makes
    the table answer questions -- "what did it say" and "what did it call" are
    different columns, not one string a query has to parse.
    """
    payload = read_json(bundle / "transcript.json") or {}
    rows = []
    for index, message in enumerate(payload.get("messages") or []):
        content = message.get("content")
        tool_calls = None
        if isinstance(content, str) and content.startswith("{"):
            try:
                inner = json.loads(content)
            except ValueError:
                inner = None
            if isinstance(inner, dict) and "tool_calls" in inner:
                tool_calls = inner.get("tool_calls")
                content = inner.get("content") or ""
        text = content if isinstance(content, str) else json_cell(content)
        rows.append({
            "trial_id": trial_id, "round": round_number, "message_index": index,
            "role": message.get("role"), "content": text,
            "tool_calls_json": json_cell(tool_calls) if tool_calls else None,
            "tool_name": message.get("tool_name"),
            "content_chars": len(text or ""),
        })
    return rows


def collect_trial(run_dir: Path, run: dict[str, Any], task_id: str,
                  prices: dict[str, Any], pricing_version: str,
                  with_events: bool) -> dict[str, list[dict[str, Any]]]:
    """Everything one task left behind, as rows."""
    task_dir = run_dir / "tasks" / task_id
    trial_id = f"{run_dir.name}/{task_id}"
    status = read_json(task_dir / "status.json") or {}
    metadata = (run.get("task_metadata") or {}).get(task_id) or {}
    relative = status.get("official_result")
    official = read_json(task_dir / relative) if relative else {}
    oracle = (official or {}).get("oracle_result") or {}
    # Written by the entrypoint after the plugin delta is applied: what this
    # container actually had, rather than what the job asked for.
    effective = read_json(task_dir / "live" / "profile.json") or {}

    out: dict[str, list[dict[str, Any]]] = {name: [] for name in RUN_SCOPED}
    model_rows: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    action_counts: dict[str, int] = {}
    repeat_count = tool_errors = assistant_chars = 0
    tokens = {"input": [], "cached": [], "output": []}

    for event_index, event in enumerate(read_jsonl(task_dir / "events.jsonl")):
        frame = event.get("frame") or {}
        if with_events:
            out["events"].append({
                "trial_id": trial_id, "event_index": event_index, "at": event.get("at"),
                "source": event.get("source"), "kind": event.get("kind"),
                "frame_kind": frame.get("kind"), "event_json": json_cell(event),
            })

        if event.get("source") == "llm" and event.get("kind") == "llm_call":
            payload = event.get("payload") or {}
            row = {
                "trial_id": trial_id, "call_index": len(model_rows) + 1,
                "at": event.get("at"), "model": payload.get("model"),
                "ok": payload.get("ok"), "duration_s": number(payload.get("duration_s")),
                "prompt_tokens": number(payload.get("prompt_tokens")),
                "cached_prompt_tokens": number(payload.get("cached_prompt_tokens")),
                "completion_tokens": number(payload.get("completion_tokens")),
                "has_tool_calls": payload.get("has_tool_calls"),
                "error": payload.get("error"),
            }
            model_rows.append(row)
            out["model_calls"].append(row)
            for bucket, field in (("input", "prompt_tokens"),
                                  ("cached", "cached_prompt_tokens"),
                                  ("output", "completion_tokens")):
                if row[field] is not None:
                    tokens[bucket].append(int(row[field]))
            continue

        if event.get("source") == "approver":
            payload = event.get("payload") or {}
            out["approvals"].append({
                "trial_id": trial_id, "at": event.get("at"),
                "kind": event.get("kind"), "choice": payload.get("choice"),
                "type": payload.get("type"), "subject": payload.get("subject"),
                "latency_s": number(payload.get("latency_s")),
                "payload_json": json_cell(payload),
            })

        payload = frame.get("payload")
        if frame.get("kind") == "stream_delta" and isinstance(payload, dict):
            assistant_chars += len(str(payload.get("delta") or ""))
        if frame.get("kind") != "tool_status" or not isinstance(payload, dict):
            continue
        call_id = str(payload.get("call_id") or f"event-{event_index}")
        name = (payload.get("tool_name") or payload.get("command_name")
                or payload.get("kind") or "tool")
        if payload.get("status") == "started":
            args = payload.get("args") or {}
            signature = hashlib.sha256(
                (str(name) + "\n" + json_cell(args)).encode()).hexdigest()[:16]
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
                "started_at": None, "is_repeated_exact_action": False,
                "args_json": "{}", "duration_s": None,
            })
            row.update({"finished_at": event.get("at"), "ok": payload.get("ok"),
                        "error": payload.get("error"), "summary": payload.get("summary")})
            if row.get("started_at") is not None and event.get("at") is not None:
                row["duration_s"] = event["at"] - row["started_at"]
            tool_errors += int(payload.get("ok") is False)
    out["tool_calls"].extend(tool_by_id.values())

    # The driver bundle: read by nothing until now, and it holds the only
    # record that the requested security mode was actually granted.
    approvals = denied = questions = 0
    granted_mode = None
    for round_number, bundle in driver_bundles(task_dir):
        result = read_json(bundle / "result.json") or {}
        metrics = result.get("metrics") or {}
        outcome = result.get("outcome") or {}
        granted_mode = ((result.get("session") or {}).get("mode")) or granted_mode
        approvals += int(metrics.get("approvals") or 0)
        denied += int(metrics.get("approvals_denied") or 0)
        questions += int(metrics.get("questions_asked") or 0)
        out["driver_rounds"].append({
            "trial_id": trial_id, "round": round_number, "ok": outcome.get("ok"),
            "reason": outcome.get("reason"), "wall_s": number(result.get("wall_s")),
            "security_mode": result.get("security_mode"), "granted_mode": granted_mode,
            "tool_calls": metrics.get("tool_calls"),
            "script_runs": metrics.get("script_runs"),
            "shell_runs": metrics.get("shell_runs"),
            "approvals": metrics.get("approvals"),
            "approvals_denied": metrics.get("approvals_denied"),
            "questions_asked": metrics.get("questions_asked"),
            "ledger_failed": metrics.get("ledger_failed"),
            "ledger_refused": metrics.get("ledger_refused"),
            "stream_deltas": metrics.get("stream_deltas"),
            "errors": metrics.get("errors"),
            "tools_json": json_cell(metrics.get("tools") or {}),
            "approvals_by_type_json": json_cell(metrics.get("approvals_by_type") or {}),
            "final_text": outcome.get("final_text"),
        })
        out["messages"].extend(messages_from(bundle, trial_id, round_number))

    for item in oracle.get("checks") or []:
        out["oracle_checks"].append({
            "trial_id": trial_id, "check_id": item.get("id"), "pass": item.get("pass"),
            "weight": number(item.get("weight")), "detail_json": json_cell(item.get("detail")),
        })

    integrity_checks = [item for item in oracle.get("checks") or []
                        if item.get("id") in {"fixture_integrity", "fixtures_unchanged",
                                              "input_integrity", "code_integrity"}]
    integrity = "n/a" if not integrity_checks else (
        "pass" if all(item.get("pass") for item in integrity_checks) else "fail")

    flags = []
    if status.get("state") != "complete":
        flags.append(str(status.get("state") or "unknown_state"))
    if status.get("adapter_ok") is False:
        flags.append("adapter_failed")
    if integrity == "fail":
        flags.append("integrity_check_failed")
    if status.get("provider_warning"):
        flags.append("provider_warning")
    # The configuration the job asked for and the one the container ran must
    # agree, or the row is describing something that did not happen.
    asked = status.get("profile") or run.get("profile")
    if effective and asked and effective.get("profile") != asked:
        flags.append("profile_mismatch")
    requested_mode = status.get("mode") or run.get("mode")
    if granted_mode and requested_mode == "yolo" and granted_mode != "yolo":
        flags.append("mode_not_granted")

    billed = sum(tokens["input"]) if tokens["input"] else None
    cached = sum(tokens["cached"]) if tokens["cached"] else None
    output = sum(tokens["output"]) if tokens["output"] else None
    calls = len(model_rows)
    input_complete = bool(calls) and len(tokens["input"]) == calls
    output_complete = bool(calls) and len(tokens["output"]) == calls
    model = status.get("model") or run.get("model")
    template = run.get("template") or {}

    score = 0.0
    if status.get("state") == "complete" and isinstance(
            status.get("outcome_score"), (int, float)) and not isinstance(
            status.get("outcome_score"), bool):
        score = float(status["outcome_score"])

    trial = {
        "trial_id": trial_id, "run_id": run_dir.name,
        "job_id": run.get("job_id"), "replicate": run.get("replicate"),
        "task_id": task_id, "title": metadata.get("title"),
        "task_class": metadata.get("class"), "difficulty": metadata.get("difficulty"),
        "rounds": metadata.get("rounds"),
        "mode": requested_mode, "model": model,
        "profile": effective.get("profile") or asked,
        "kernel_commit": effective.get("kernel_commit") or template.get("kernel_commit"),
        "store_commit": effective.get("store_commit") or template.get("store_commit"),
        "benchmark_commit": run.get("benchmark_commit"),
        "state": status.get("state"), "outcome_score": number(status.get("outcome_score")),
        "score": score, "combined_score": number(status.get("combined_score")),
        "adapter_ok": status.get("adapter_ok"),
        "elapsed_sec": number(status.get("elapsed_sec")),
        "model_time_sec": round(sum(
            row["duration_s"] for row in model_rows
            if row.get("duration_s") is not None), 3) if model_rows else None,
        "model_calls": calls,
        "input_tokens_billed": billed,
        "input_tokens_largest_call": max(tokens["input"]) if tokens["input"] else None,
        "cached_input_tokens": cached,
        "input_tokens_uncached": (billed - (cached or 0)) if billed is not None else None,
        "output_tokens": output,
        "input_complete": input_complete, "output_complete": output_complete,
        "tokens_complete": input_complete and output_complete,
        "pricing_version": pricing_version,
        "tool_calls": len(tool_by_id), "tool_errors": tool_errors,
        "repeated_exact_actions": repeat_count,
        "assistant_stream_chars": assistant_chars,
        "run_script_calls": sum(row.get("tool_name") == "run_script"
                                for row in tool_by_id.values()),
        "validate_calls": sum(row.get("tool_name") == "validate"
                              for row in tool_by_id.values()),
        "shell_calls": sum(row.get("tool_name") == "run_command"
                           for row in tool_by_id.values()),
        "approvals": approvals, "approvals_denied": denied,
        "questions_asked": questions,
        "input_integrity": integrity, "validity_flags": ";".join(flags),
        "message_count": len(out["messages"]),
        "round_count": len(out["driver_rounds"]),
        "tool_names_json": json_cell(effective.get("tool_names")) if effective else None,
        "transcript_path": str((task_dir / "sandboxes").resolve()),
        "events_path": str((task_dir / "events.jsonl").resolve()),
        "official_result_path": str((task_dir / relative).resolve()) if relative else None,
    }
    trial.update(costs(model, billed, cached, output, prices))
    out["trials"].append(trial)
    return out


# -- the database -----------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    for table, columns in SCHEMA.items():
        spec = ", ".join(f'"{name}" {kind}' for name, kind in columns.items())
        connection.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({spec})')
    for table in RUN_SCOPED:
        if table == "trials":
            continue
        connection.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{table}_trial" ON "{table}" (trial_id)')
    connection.execute(
        'CREATE INDEX IF NOT EXISTS "idx_trials_run" ON "trials" (run_id)')
    for name, body in VIEWS.items():
        connection.execute(f'DROP VIEW IF EXISTS "{name}"')
        connection.execute(f'CREATE VIEW "{name}" AS {body}')
    connection.commit()
    return connection


def insert(connection: sqlite3.Connection, table: str,
           rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(SCHEMA[table])
    placeholders = ",".join("?" for _ in columns)
    quoted = ",".join(f'"{name}"' for name in columns)
    payload = []
    for row in rows:
        record = []
        for name in columns:
            value = row.get(name)
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, (dict, list)):
                value = json_cell(value)
            record.append(value)
        payload.append(record)
    connection.executemany(
        f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})', payload)


def export_runs(run_dirs: list[Path], output: Path, *, with_events: bool = False,
                rebuild: bool = False, csv_dir: Path | None = None,
                prune: bool = False) -> dict[str, Any]:
    """Refresh the database for these runs and return a manifest.

    ``prune`` drops rows for runs and jobs that are no longer on disk. It is
    only safe when ``run_dirs`` is the *whole* corpus -- pruning during a
    single-run export would delete every other run -- so the CLI sets it
    exactly when no ``--run`` filter was given.
    """
    output.mkdir(parents=True, exist_ok=True)
    database = output / DATABASE_NAME
    prices, pricing_version = pricing_table()
    connection = connect(database)
    try:
        known = {row[0]: row[1] for row in
                 connection.execute("SELECT run_id, fingerprint FROM source_runs")}
        refreshed, skipped = [], []
        totals = {name: 0 for name in RUN_SCOPED}
        csv_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in RUN_SCOPED}

        for run_dir in run_dirs:
            run = read_json(run_dir / "run.json") or {}
            signature = fingerprint(run_dir)
            if not rebuild and known.get(run_dir.name) == signature and not csv_dir:
                skipped.append(run_dir.name)
                continue

            # Delete-then-insert, so a re-exported run replaces its rows rather
            # than accumulating a second copy beside them.
            for table in RUN_SCOPED:
                key = "run_id" if table == "trials" else "trial_id"
                if table == "trials":
                    connection.execute('DELETE FROM "trials" WHERE run_id = ?',
                                       (run_dir.name,))
                else:
                    connection.execute(
                        f'DELETE FROM "{table}" WHERE {key} LIKE ?',
                        (run_dir.name + "/%",))

            summary = read_json(run_dir / "summary.json") or {}
            job_id, replicate = split_run_id(run_dir.name)
            run.setdefault("job_id", job_id)
            run.setdefault("replicate", replicate)
            insert(connection, "runs", [{
                "run_id": run_dir.name, "job_id": job_id, "replicate": replicate,
                "created_at": number(run.get("created_at")),
                "mode": run.get("mode"), "model": run.get("model"),
                "profile": run.get("profile"), "tool_profile": run.get("tool_profile"),
                "visible_tools_json": json_cell(run.get("visible_tools")),
                "kernel_commit": (run.get("template") or {}).get("kernel_commit"),
                "store_commit": (run.get("template") or {}).get("store_commit"),
                "benchmark_commit": run.get("benchmark_commit"),
                "image": run.get("image"), "image_id": run.get("image_id"),
                "task_count": len(run.get("tasks") or []),
                "completion_score": number(summary.get("completion_score")),
            }])

            for task_id in run.get("tasks") or []:
                rows = collect_trial(run_dir, run, task_id, prices,
                                     pricing_version, with_events)
                for table, values in rows.items():
                    insert(connection, table, values)
                    totals[table] += len(values)
                    if csv_dir:
                        csv_rows[table].extend(values)

            insert(connection, "source_runs", [{
                "run_id": run_dir.name, "fingerprint": signature,
                "exported_at": time.time(), "run_path": str(run_dir.resolve()),
            }])
            refreshed.append(run_dir.name)

        jobs = job_rows(output)
        if prune:
            # A run directory or job file deleted on disk must not leave rows
            # behind: the database claims to be derived from what is there,
            # and a stale trial would keep contributing to every mean.
            keep = {path.name for path in run_dirs}
            stale = [row[0] for row in connection.execute("SELECT run_id FROM runs")
                     if row[0] not in keep]
            for run_id in stale:
                connection.execute('DELETE FROM "trials" WHERE run_id = ?', (run_id,))
                for table in RUN_SCOPED:
                    if table != "trials":
                        connection.execute(f'DELETE FROM "{table}" WHERE trial_id LIKE ?',
                                           (run_id + "/%",))
                connection.execute('DELETE FROM "runs" WHERE run_id = ?', (run_id,))
                connection.execute('DELETE FROM "source_runs" WHERE run_id = ?', (run_id,))
            connection.execute("DELETE FROM jobs")
        insert(connection, "jobs", jobs)
        connection.commit()
        counts = {table: connection.execute(
            f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in SCHEMA}
    finally:
        connection.close()

    if csv_dir:
        csv_dir.mkdir(parents=True, exist_ok=True)
        for table, rows in csv_rows.items():
            if rows:
                write_csv(csv_dir / f"{table}.csv", rows, list(SCHEMA[table]))

    manifest = {
        "schema_version": 2, "generated_at": time.time(),
        "database": str(database), "runs_refreshed": refreshed,
        "runs_skipped_unchanged": skipped, "rows_written": totals,
        "table_counts": counts, "pricing_version": pricing_version,
        "events_loaded": with_events,
        "notes": [
            "input_tokens_billed sums each call's whole prompt: it is billed "
            "input, not context size. Use input_tokens_largest_call for that.",
            "cached_input_tokens is the discounted share OF billed input, "
            "never an addition to it.",
            "A NULL cost means a missing price or a missing count, never zero.",
            "config_scores averages per task before averaging across tasks, so "
            "unevenly repeated tasks do not skew the headline number.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def split_run_id(run_id: str) -> tuple[str | None, int | None]:
    """``<job>-r3`` -> ``("<job>", 3)``; anything else has no job."""
    head, _, tail = run_id.rpartition("-r")
    if head and tail.isdigit():
        return head, int(tail)
    return None, None


def job_rows(results_root: Path) -> list[dict[str, Any]]:
    folder = results_root / "jobs"
    if not folder.is_dir():
        return []
    rows = []
    for job_dir in sorted(folder.iterdir()):
        payload = read_json(job_dir / "job.json")
        if not payload:
            continue
        spec = payload.get("spec") or {}
        rows.append({
            "job_id": payload.get("job_id"), "state": payload.get("state"),
            "model": spec.get("model"), "profile": spec.get("profile"),
            "mode": spec.get("mode"), "repeats": spec.get("repeats"),
            "task_count": len(payload.get("tasks") or []),
            "trial_count": payload.get("trial_count"),
            "selector_json": json_cell(spec.get("tasks")),
            "notes": spec.get("notes"), "created_at": number(payload.get("created_at")),
            "updated_at": number(payload.get("updated_at")),
            "paused_reason": payload.get("paused_reason"),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="append",
                        help="run id or path; repeat, or omit for every run")
    parser.add_argument("--output", default=str(RESULTS),
                        help=f"directory holding {DATABASE_NAME}")
    parser.add_argument("--with-events", action="store_true",
                        help="also load every raw event row (large)")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-export even runs whose fingerprint is unchanged")
    parser.add_argument("--csv", metavar="DIR", help="also write flat CSVs here")
    options = parser.parse_args(argv)

    if options.run:
        run_dirs = [Path(value).resolve() if Path(value).exists() else RESULTS / value
                    for value in options.run]
    else:
        run_dirs = sorted(path for path in RESULTS.iterdir()
                          if path.is_dir() and (path / "run.json").exists())
    run_dirs = [path for path in run_dirs if (path / "run.json").exists()]
    if not run_dirs:
        parser.error("no Harness-Bench runs found")

    manifest = export_runs(
        run_dirs, Path(options.output).resolve(),
        with_events=options.with_events, rebuild=options.rebuild,
        csv_dir=Path(options.csv).resolve() if options.csv else None,
        # Only a whole-corpus export knows what "no longer on disk" means.
        prune=not options.run)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
