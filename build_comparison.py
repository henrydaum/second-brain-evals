"""Join the six Second Brain runs to the published Harness-Bench data and emit the CSVs.

Reads only derived artefacts and the archived published JSON, so it can be
re-run at any time without touching the read-only snapshot.

Two scoring variants are emitted side by side and never mixed:

``paper-comparable``
    Task 012 scored with the **upstream** oracle, bugs and all, because the
    published paper was produced that way. This is the only variant that may be
    compared against another harness's published figure.
``pr7-corrected``
    Tasks 012/014 scored with Qihoo360/harness-bench PR #7 applied. Research
    interest only.

Both variants carry the re-graded process scores, because a failed judge is a
measurement failure rather than a scoring convention.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DERIVED = ROOT / "results" / "study-2026-08" / "derived"

OUR_MODELS = {
    "deepseek-ai/deepseek-v4-flash": "deepseek-v4-flash",
    "qwen/qwen3.6-plus": "qwen3.6-plus",
    "moonshotai/kimi-k2.5": "kimi-k2.5",
}
TARGET_MODELS = tuple(OUR_MODELS.values())
VARIANTS = ("paper-comparable", "pr7-corrected")


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pct(value: float | None) -> str:
    return "" if value is None else f"{100 * value:.1f}"


def load_regrades(derived: Path) -> dict[tuple[str, str], dict]:
    """(run_id, task_id) -> the re-graded scoring block."""
    out: dict[tuple[str, str], dict] = {}
    d = derived / "regraded"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*__*.json")):
        blob = json.loads(f.read_text(encoding="utf-8"))
        out[(blob["run_id"], blob["task_id"])] = blob.get("scoring") or {}
    return out


def load_oracle_variants(derived: Path) -> dict[tuple[str, str], dict]:
    p = derived / "oracle_variants.json"
    if not p.is_file():
        return {}
    return {(r["run_id"], r["task_id"]): r for r in json.loads(p.read_text(encoding="utf-8"))}


def load_overrides(derived: Path) -> dict[tuple[str, str], dict]:
    """Per-trial score overrides, each carrying its own evidence. See the file."""
    p = derived / "manual_overrides.json"
    if not p.is_file():
        return {}
    blob = json.loads(p.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict] = {}
    for key, value in blob.items():
        if key.startswith("_") or "|" not in key:
            continue
        run_id, task_id = key.split("|", 1)
        out[(run_id, task_id)] = value
    return out


def second_brain_rows(derived: Path) -> tuple[list[dict], dict]:
    """One assembled row per (run, task, variant), plus per-run metadata."""
    con = sqlite3.connect(derived / "harness_bench.sqlite")
    con.row_factory = sqlite3.Row
    trials = con.execute("""
        SELECT run_id, task_id, model, mode, state, outcome_score, process_score,
               security_score, combined_score, model_calls, tool_calls,
               input_tokens_billed, cached_input_tokens, output_tokens,
               cost_total_usd, elapsed_sec
        FROM trials ORDER BY run_id, task_id""").fetchall()
    con.close()

    regrades = load_regrades(derived)
    variants = load_oracle_variants(derived)
    overrides = load_overrides(derived)

    rows: list[dict] = []
    meta: dict[str, dict] = {}
    for t in trials:
        key = (t["run_id"], t["task_id"])
        meta.setdefault(t["run_id"], {"model": t["model"], "mode": t["mode"]})
        ov = overrides.get(key, {})

        rg = regrades.get(key)
        judge_failed_originally = t["process_score"] is None
        if ov.get("process_score") is not None:
            process, security, regraded = ov["process_score"], ov.get("security_score"), False
        elif rg is not None:
            process = rg.get("process_score")
            security = rg.get("security_score")
            regraded = True
        else:
            process, security, regraded = t["process_score"], t["security_score"], False

        var = variants.get(key)
        for variant in VARIANTS:
            completion = ov.get("outcome_score", t["outcome_score"])
            if var is not None:
                completion = var.get("upstream_score") if variant == "paper-comparable" \
                    else var.get("pr7_score")
            if completion is None:
                combined = None
            else:
                combined = completion * (process if process is not None else 1.0) \
                    * (security if security is not None else 1.0)
            rows.append({
                "run_id": t["run_id"], "task_id": t["task_id"],
                "model": OUR_MODELS.get(t["model"], t["model"]), "mode": t["mode"],
                "variant": variant, "state": ov.get("state", t["state"]),
                "overridden": bool(ov),
                "completion": completion, "process": process, "combined": combined,
                "regraded": regraded, "judge_failed_originally": judge_failed_originally,
                "model_calls": t["model_calls"], "tool_calls": t["tool_calls"],
                "input_tokens_billed": t["input_tokens_billed"],
                "cached_input_tokens": t["cached_input_tokens"],
                "output_tokens": t["output_tokens"],
                "cost_total_usd": t["cost_total_usd"],
            })
    return rows, meta


def aggregate_second_brain(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[(r["run_id"], r["variant"])].append(r)

    for (run_id, variant), items in sorted(groups.items()):
        m, mode = items[0]["model"], items[0]["mode"]
        comb = [i["combined"] for i in items if i["combined"] is not None]
        compl = [i["completion"] for i in items if i["completion"] is not None]
        proc = [i["process"] for i in items if i["process"] is not None]
        s = lambda k: sum(i[k] or 0 for i in items)  # noqa: E731
        billed, out_tok = s("input_tokens_billed"), s("output_tokens")
        cached = s("cached_input_tokens")
        n = len(items)
        still_failing = sum(1 for i in items if i["process"] is None)
        run_proc = mean(proc)
        adj = []
        for i in items:
            if i["combined"] is None:
                continue
            if i["process"] is None and i["completion"] is not None and run_proc is not None:
                adj.append(i["completion"] * run_proc)
            else:
                adj.append(i["combined"])
        out.append({
            "harness": "second-brain", "model": m, "mode": mode,
            "scoring_variant": variant, "n_tasks": n, "n_process_scored": len(proc),
            "combined": mean(comb), "completion": mean(compl), "process": mean(proc),
            "combined_judge_adjusted": mean(adj),
            "input_tokens": billed, "output_tokens": out_tok,
            "cache_read_tokens": cached or None,
            "total_tokens": billed + out_tok,
            "turns": s("model_calls"), "tool_calls": s("tool_calls"),
            "judge_failure_rate": still_failing / n if n else None,
            "cost_usd": sum(i["cost_total_usd"] or 0 for i in items),
            "run_id": run_id,
        })
    return out


def rival_rows(derived: Path) -> list[dict]:
    pub = derived / "published"
    scores = json.loads((pub / "leaderboard_scores.json").read_text(encoding="utf-8"))["runs"]
    usage = json.loads((pub / "usage_summary.json").read_text(encoding="utf-8"))["by_pair"]

    agg: dict[tuple[str, str], dict[str, list]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for r in scores:
        if r["model"] not in TARGET_MODELS and not (r["harness"] == "codex"):
            continue
        for field in ("combined", "completion", "process"):
            if r[field] is not None:
                agg[(r["harness"], r["model"])][field].append(r[field])

    out: list[dict] = []
    for (harness, model), vals in sorted(agg.items()):
        u = usage.get(f"{harness}|{model}") or {}
        is_codex = harness == "codex"
        cache_read = u.get("cache_read_tokens")
        # codex input already includes cache reads; every other harness reports
        # uncached input with cache reads additive.
        billed = u.get("input_tokens") if is_codex else \
            (u.get("input_tokens", 0) + (cache_read or 0))
        out_tok = u.get("output_tokens")
        n = len(vals["combined"])
        pair_rows = [r for r in scores if r["harness"] == harness and r["model"] == model]
        nulls = sum(1 for r in pair_rows if r["process"] is None)

        # Like-for-like: a failed judge is credited process_effective = 1.0, which
        # is a free perfect grade rather than a measurement. Second Brain's
        # failures were re-graded away, so leaving the rivals' in place would bias
        # the comparison in our favour -- backwards. Impute each pair's OWN mean
        # observed process onto its unjudged trials instead of ours, so the
        # adjustment never imports our behaviour into their number.
        pair_proc = mean(vals["process"])
        adj: list[float] = []
        for r in pair_rows:
            if r["combined"] is None:
                continue
            if r["process"] is None and r["completion"] is not None and pair_proc is not None:
                adj.append(r["completion"] * pair_proc)
            else:
                adj.append(r["combined"])
        out.append({
            "harness": harness, "model": model, "mode": "",
            "scoring_variant": "published", "n_tasks": n,
            "n_process_scored": len(vals["process"]),
            "combined": mean(vals["combined"]), "completion": mean(vals["completion"]),
            "process": mean(vals["process"]), "combined_judge_adjusted": mean(adj),
            "input_tokens": billed, "output_tokens": out_tok,
            "cache_read_tokens": None if is_codex else cache_read,
            "total_tokens": (billed or 0) + (out_tok or 0),
            "turns": None, "tool_calls": None,
            "judge_failure_rate": nulls / u.get("result_count", n) if u.get("result_count") else None,
            "cost_usd": None, "run_id": "",
        })
    return out


FIELDS = ["harness", "model", "mode", "scoring_variant", "n_tasks", "n_process_scored",
          "combined", "combined_judge_adjusted", "completion", "process", "input_tokens", "output_tokens",
          "cache_read_tokens", "total_tokens", "tokens_per_task", "turns", "tool_calls",
          "judge_failure_rate", "cost_usd", "run_id"]


def finish(row: dict) -> dict:
    r = dict(row)
    n = r.get("n_tasks") or 0
    r["tokens_per_task"] = round(r["total_tokens"] / n) if n and r.get("total_tokens") else ""
    for k in ("combined", "combined_judge_adjusted", "completion", "process"):
        r[k] = pct(r[k])
    r["judge_failure_rate"] = "" if r["judge_failure_rate"] is None \
        else f"{100 * r['judge_failure_rate']:.1f}"
    r["cost_usd"] = "" if not r.get("cost_usd") else f"{r['cost_usd']:.4f}"
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens",
              "turns", "tool_calls"):
        r[k] = "" if r.get(k) in (None, 0) else r[k]
    return {k: r.get(k, "") for k in FIELDS}


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(finish(r))
    print(f"wrote {path}  ({len(rows)} rows)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--derived", default=str(DERIVED))
    opts = ap.parse_args(argv)
    derived = Path(opts.derived).resolve()

    trials, _ = second_brain_rows(derived)
    sb = aggregate_second_brain(trials)
    rivals = rival_rows(derived)

    # by_harness: grouped by harness, then model. Second Brain first.
    order = {"second-brain": 0}
    by_harness = sorted(sb + rivals,
                        key=lambda r: (order.get(r["harness"], 1), r["harness"],
                                       r["model"], r["mode"], r["scoring_variant"]))
    write_csv(derived / "by_harness.csv", by_harness)

    # by_model: grouped by model, ranked within it. Codex has no matching model,
    # so it lands in its own gpt-5.4 block as an indirect reference.
    by_model = sorted(sb + rivals,
                      key=lambda r: (r["model"], -(float(r["combined"] or 0)),
                                     r["harness"], r["mode"]))
    write_csv(derived / "by_model.csv", by_model)

    # Per-trial detail, for Part III.
    detail = derived / "second_brain_trials.csv"
    keys = ["run_id", "model", "mode", "variant", "task_id", "state", "completion",
            "process", "combined", "regraded", "overridden", "judge_failed_originally", "model_calls",
            "tool_calls", "input_tokens_billed", "cached_input_tokens", "output_tokens",
            "cost_total_usd"]
    with detail.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in trials:
            w.writerow({k: r.get(k, "") for k in keys})
    print(f"wrote {detail}  ({len(trials)} rows)")

    # Cost is Second Brain only: rival prices are unknown, and our Atlas Cloud
    # rates measure the vendor rather than the harness.
    cost = derived / "second_brain_cost.csv"
    ck = ["model", "mode", "run_id", "n_tasks", "input_tokens_billed", "cached_input_tokens",
          "cache_hit_pct", "output_tokens", "cost_usd", "cost_per_task", "combined",
          "cost_per_score_point"]
    with cost.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ck)
        w.writeheader()
        for r in sb:
            if r["scoring_variant"] != "paper-comparable":
                continue
            billed, cached = r["input_tokens"], r["cache_read_tokens"]
            comb = r["combined"]
            w.writerow({
                "model": r["model"], "mode": r["mode"], "run_id": r["run_id"],
                "n_tasks": r["n_tasks"], "input_tokens_billed": billed,
                "cached_input_tokens": cached or "",
                "cache_hit_pct": f"{100 * cached / billed:.1f}" if cached and billed else "",
                "output_tokens": r["output_tokens"],
                "cost_usd": f"{r['cost_usd']:.4f}",
                "cost_per_task": f"{r['cost_usd'] / r['n_tasks']:.4f}",
                "combined": pct(comb),
                "cost_per_score_point": f"{r['cost_usd'] / (100 * comb):.4f}" if comb else "",
            })
    print(f"wrote {cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
