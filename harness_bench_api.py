"""Stable programmatic API for running Harness-Bench against Second Brain."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from export_harness_bench_data import RESULTS, export_runs, read_json
from run_harness_bench import DEFAULT_BENCHMARK, DEFAULT_IMAGE, validate_benchmark


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RunRequest:
    task_ids: list[str]
    mode: str = "yolo"
    env_file: str = "bench.env"
    image: str = DEFAULT_IMAGE
    run_id: str | None = None
    skip_provider_check: bool = False


@dataclass(frozen=True)
class RunResponse:
    run_id: str
    exit_code: int
    run_dir: str
    dataset_dir: str
    database: str
    run: dict[str, Any]
    summary: dict[str, Any]
    trials: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HarnessBenchAPI:
    """Run tasks and return both headline results and normalized raw data."""

    def __init__(self, *, benchmark_root: Path = DEFAULT_BENCHMARK,
                 results_root: Path = RESULTS) -> None:
        self.benchmark_root = Path(benchmark_root).resolve()
        self.results_root = Path(results_root).resolve()

    def list_tasks(self) -> list[dict[str, Any]]:
        metadata = validate_benchmark(self.benchmark_root)
        return [{"task_id": task_id, **details}
                for task_id, details in metadata["tasks"].items()]

    def run_tasks(self, request: RunRequest) -> RunResponse:
        """Synchronously execute fresh-container trials and return their dataset."""
        if not request.task_ids:
            raise ValueError("task_ids must not be empty")
        if request.mode not in {"yolo", "lockdown", "mediated"}:
            raise ValueError(f"unsupported mode: {request.mode}")
        available = {row["task_id"] for row in self.list_tasks()}
        unknown = sorted(set(request.task_ids) - available)
        if unknown:
            raise ValueError("unknown Harness-Bench tasks: " + ", ".join(unknown))
        run_id = request.run_id or self._new_run_id()
        command = [
            sys.executable, str(ROOT / "run_harness_bench.py"),
            "--benchmark-root", str(self.benchmark_root),
            "--env-file", request.env_file, "--image", request.image,
            "--mode", request.mode, "--execute", "--run-id", run_id,
        ]
        for task_id in request.task_ids:
            command.extend(("--task", task_id))
        if request.skip_provider_check:
            command.append("--skip-provider-check")
        completed = subprocess.run(command, cwd=ROOT, check=False)
        return self.get_run(run_id, exit_code=completed.returncode, export=True)

    def get_run(self, run_id: str, *, exit_code: int = 0,
                export: bool = False) -> RunResponse:
        """Return a completed or in-progress run; optionally refresh its dataset."""
        run_dir = self.results_root / run_id
        run = read_json(run_dir / "run.json")
        if not run:
            raise FileNotFoundError(f"Harness-Bench run not found: {run_id}")
        summary = read_json(run_dir / "summary.json") or {
            "run_id": run_id, "task_count": len(run.get("tasks") or []), "tasks": []
        }
        dataset_dir = run_dir / "dataset"
        database = dataset_dir / "harness_bench.sqlite"
        if export or not database.exists():
            export_runs([run_dir], dataset_dir)
        trials = self._rows(database, "SELECT * FROM trials ORDER BY task_id")
        return RunResponse(
            run_id=run_id, exit_code=exit_code, run_dir=str(run_dir),
            dataset_dir=str(dataset_dir), database=str(database),
            run=run, summary=summary, trials=trials,
        )

    @staticmethod
    def _rows(database: Path, query: str) -> list[dict[str, Any]]:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(query)]
        finally:
            connection.close()

    @staticmethod
    def _new_run_id() -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return f"api-{stamp}-{uuid.uuid4().hex[:8]}"


def main(argv: list[str] | None = None) -> int:
    """Small JSON command surface for languages that do not import Python."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("tasks")
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("run_id")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--task", action="append", required=True)
    run_parser.add_argument("--mode", choices=("yolo", "lockdown", "mediated"), default="yolo")
    run_parser.add_argument("--env-file", default="bench.env")
    run_parser.add_argument("--run-id")
    options = parser.parse_args(argv)
    api = HarnessBenchAPI()
    if options.action == "tasks":
        result: Any = api.list_tasks()
    elif options.action == "get":
        result = api.get_run(options.run_id, export=True).to_dict()
    else:
        result = api.run_tasks(RunRequest(
            task_ids=options.task, mode=options.mode,
            env_file=options.env_file, run_id=options.run_id,
        )).to_dict()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
