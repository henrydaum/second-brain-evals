"""Produce an exception-aware scorecard from a completed Harbor job."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def summarize(job_dir: Path) -> dict:
    job = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    trials = []
    for path in sorted(job_dir.glob("*/result.json")):
        try:
            trials.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue

    total = int(job.get("n_total_trials") or len(trials))
    rewards = []
    errors = []
    metrics = []
    outcomes: dict[str, int] = {}
    for trial in trials:
        exception = trial.get("exception_info")
        if exception:
            errors.append({"task": trial.get("task_name"), "exception": exception})
        reward_map = (trial.get("verifier_result") or {}).get("rewards") or {}
        if isinstance(reward_map.get("reward"), (int, float)):
            rewards.append(float(reward_map["reward"]))
        sb = (((trial.get("agent_result") or {}).get("metadata") or {})
              .get("second_brain") or {})
        outcome = (sb.get("outcome") or {}).get("reason")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if isinstance(sb.get("metrics"), dict):
            metrics.append(sb["metrics"])

    # The conservative framework score counts every scheduled trial. Harbor's
    # mean may exclude exceptions because no verifier reward exists for them.
    conservative = sum(rewards) / total if total else None
    verifier_mean = statistics.mean(rewards) if rewards else None
    return {
        "job_id": job.get("id"),
        "scheduled_trials": total,
        "trial_results": len(trials),
        "verifier_rewards": len(rewards),
        "exceptions": len(errors),
        "reward_sum": sum(rewards),
        "score_all_scheduled": conservative,
        "mean_scored_trials": verifier_mean,
        "outcomes": outcomes,
        "mean_tool_calls": _mean(metrics, "tool_calls"),
        "mean_approvals": _mean(metrics, "approvals"),
        "mean_llm_calls": _nested_mean(metrics, "llm", "calls"),
        "known_prompt_tokens": _nested_sum(
            metrics, "llm", "prompt_tokens_total_known"
        ),
        "trials_with_prompt_token_telemetry": sum(
            1
            for row in metrics
            if isinstance((row.get("llm") or {}).get("prompt_tokens_total_known"), (int, float))
        ),
        "errors": errors,
    }


def _mean(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(statistics.mean(values), 2) if values else None


def _nested_mean(rows: list[dict], group: str, key: str) -> float | None:
    values = [
        row[group][key]
        for row in rows
        if isinstance(row.get(group), dict)
        and isinstance(row[group].get(key), (int, float))
    ]
    return round(statistics.mean(values), 2) if values else None


def _nested_sum(rows: list[dict], group: str, key: str) -> int | float | None:
    values = [
        row[group][key]
        for row in rows
        if isinstance(row.get(group), dict)
        and isinstance(row[group].get(key), (int, float))
    ]
    return sum(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--write", action="store_true", help="also write scorecard.json")
    options = parser.parse_args()
    result = summarize(options.job_dir.resolve())
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if options.write:
        (options.job_dir / "scorecard.json").write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
