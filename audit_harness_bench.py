"""Report the concrete workload shape of the pinned Harness-Bench release."""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

import yaml

from run_harness_bench import DEFAULT_BENCHMARK, SELECTIONS, validate_benchmark


MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".wav", ".mp4", ".mov"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-root", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--output")
    options = parser.parse_args(argv)
    report = audit(Path(options.benchmark_root).resolve())
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if options.output:
        target = Path(options.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0


def audit(root: Path) -> dict[str, Any]:
    release = validate_benchmark(root)
    categories: collections.Counter[str] = collections.Counter()
    difficulties: collections.Counter[str] = collections.Counter()
    rounds: collections.Counter[int] = collections.Counter()
    extensions: collections.Counter[str] = collections.Counter()
    rows = []
    hook_tasks = []
    local_http_tasks = []
    fixture_bytes = fixture_files = 0

    for manifest in sorted((root / "tasks").glob("*/task.yaml")):
        task = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        task_id = str(task.get("task_id") or manifest.parent.name)
        categories[str(task.get("class") or "unspecified")] += 1
        difficulties[str(task.get("difficulty") or "unspecified")] += 1
        round_count = len(task.get("prompt_files") or []) or 1
        rounds[round_count] += 1
        fixture_root = manifest.parent / str(task.get("fixtures_dir") or "fixtures")
        files = [path for path in fixture_root.rglob("*") if path.is_file()] if fixture_root.exists() else []
        size = sum(path.stat().st_size for path in files)
        fixture_bytes += size
        fixture_files += len(files)
        for path in files:
            extensions[path.suffix.lower() or "<none>"] += 1
        hook = manifest.parent / "hooks.py"
        if hook.is_file():
            hook_tasks.append(task_id)
            source = hook.read_text(encoding="utf-8", errors="replace")
            if re.search(r"HTTPServer|http\.server|127\.0\.0\.1", source, re.IGNORECASE):
                local_http_tasks.append(task_id)
        rows.append({
            "task_id": task_id,
            "fixture_bytes": size,
            "fixture_files": len(files),
            "rounds": round_count,
            "media_bytes": sum(path.stat().st_size for path in files if path.suffix.lower() in MEDIA_EXTENSIONS),
        })

    non_media = [
        {**row, "non_media_bytes": row["fixture_bytes"] - row["media_bytes"]}
        for row in rows
    ]
    return {
        "benchmark_commit": release["commit"],
        "tasks": release["task_count"],
        "categories": dict(sorted(categories.items())),
        "difficulties": dict(sorted(difficulties.items())),
        "rounds": {str(key): value for key, value in sorted(rounds.items())},
        "fixture_files": fixture_files,
        "fixture_bytes": fixture_bytes,
        "fixture_extensions": dict(extensions.most_common()),
        "tasks_with_hooks": len(hook_tasks),
        "local_http_tasks": local_http_tasks,
        "largest_fixture_tasks": sorted(rows, key=lambda row: row["fixture_bytes"], reverse=True)[:10],
        "largest_non_media_task": max(non_media, key=lambda row: row["non_media_bytes"]),
        "smoke": selection_cost(rows, SELECTIONS["smoke"]),
        "pilot": selection_cost(rows, SELECTIONS["pilot"]),
    }


def selection_cost(rows: list[dict[str, Any]], task_ids: list[str]) -> dict[str, Any]:
    selected = [row for row in rows if row["task_id"] in task_ids]
    return {
        "task_ids": task_ids,
        "tasks": len(selected),
        "fixture_files": sum(row["fixture_files"] for row in selected),
        "fixture_bytes": sum(row["fixture_bytes"] for row in selected),
        "model_turns_minimum": sum(row["rounds"] for row in selected),
    }


if __name__ == "__main__":
    raise SystemExit(main())
