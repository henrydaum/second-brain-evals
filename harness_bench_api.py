"""Configure a benchmark job, run it, and collect its data.

A **job** is a configuration: a model, a plugin profile, a permission mode, a
task selection, and how many times to repeat it. Running a job produces
**trials**, one per task per replicate, and every trial is joined back to the
configuration that produced it -- including the kernel commit, so results from
different builds never silently average together.

    from harness_bench_api import HarnessBenchAPI, JobSpec

    api = HarnessBenchAPI()
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"difficulty": ["easy"]},
                           profile="bench", mode="yolo", repeats=3))
    api.run(job.job_id)                  # resumable; survives a usage limit
    api.dataset()                        # every run on disk -> SQLite

**A replicate is an ordinary run.** Replicate *n* of job ``J`` lives in the run
directory ``J-r{n}`` in the existing layout, which is why ``--resume``, the
viewer, ``compare_harness_runs.py`` and the exporter all keep working on it
unchanged. The job file records which runs belong together; it never becomes a
second, competing account of what happened. Trial state is always *derived*
from the run directories, so there is exactly one source of truth.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from export_harness_bench_data import RESULTS, export_runs, read_json
from run_harness_bench import (
    DEFAULT_BENCHMARK, DEFAULT_IMAGE, MODELS, PROFILES, choose_tasks,
    task_score, validate_benchmark,
)

ROOT = Path(__file__).resolve().parent
JOBS = RESULTS / "jobs"

#: Exit codes ``run_harness_bench.py`` uses to say "stop, but nothing is
#: broken". Both leave the run directory resumable, so the job pauses rather
#: than failing -- hitting a usage limit is the expected way a long benchmark
#: proceeds, not an error to recover from.
PAUSE_EXITS = {75: "provider_unavailable", 130: "interrupted"}

MODES = ("yolo", "lockdown", "mediated")
#: Selector keys ``JobSpec.tasks`` accepts. At least one must be present: a
#: selector that specifies nothing would silently fall back to the smoke set,
#: and a job that quietly ran two tasks instead of a hundred looks identical
#: to one that finished.
SELECTOR_KEYS = ("ids", "difficulty", "class", "all", "pilot", "smoke")


@dataclass(frozen=True)
class JobSpec:
    """Everything that decides what a job measures."""

    model: str
    tasks: dict[str, Any] = field(default_factory=dict)
    profile: str = "bench"
    mode: str = "yolo"
    #: Model that grades process and security, or ``"none"`` to leave both
    #: pinned at 1.0. Recorded per job because it is a control variable: two
    #: jobs graded by different judges are not comparable.
    judge: str = "none"
    repeats: int = 1
    #: How many tasks run at once. Recorded on the job because it changes the
    #: wall-clock numbers and nothing else: scores are per-task and unaffected,
    #: but comparing "how long did the suite take" across two jobs that ran at
    #: different concurrencies is comparing two different questions.
    concurrency: int = 1
    env_file: str = "bench.env"
    image: str = DEFAULT_IMAGE
    notes: str = ""

    def validate(self) -> None:
        if self.model not in MODELS:
            raise ValueError(f"unknown model {self.model!r}; models.json knows: "
                             + ", ".join(sorted(MODELS)))
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}; profiles.json knows: "
                             + ", ".join(sorted(PROFILES)))
        if self.mode not in MODES:
            raise ValueError(f"unsupported mode {self.mode!r}; expected one of "
                             + ", ".join(MODES))
        if self.judge != "none" and self.judge not in MODELS:
            raise ValueError(f"unknown judge {self.judge!r}; models.json knows: "
                             + ", ".join(sorted(MODELS)) + ", or 'none'")
        if self.repeats < 1:
            raise ValueError("repeats must be at least 1")
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        unknown = sorted(set(self.tasks) - set(SELECTOR_KEYS) - {"exclude"})
        if unknown:
            raise ValueError("unknown task selector key(s): " + ", ".join(unknown)
                             + "; expected any of " + ", ".join(SELECTOR_KEYS))
        if not any(self.tasks.get(key) for key in SELECTOR_KEYS):
            raise ValueError(
                "task selector chooses nothing; set one of " + ", ".join(SELECTOR_KEYS))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Job:
    job_id: str
    spec: JobSpec
    tasks: list[str]
    runs: list[str]
    state: str
    path: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def trial_count(self) -> int:
        return len(self.tasks) * self.spec.repeats

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "spec": self.spec.to_dict(),
                "tasks": self.tasks, "runs": self.runs, "state": self.state,
                "path": self.path, "trial_count": self.trial_count,
                **self.detail}


class HarnessBenchAPI:
    """Plan jobs, run them, and turn what they leave on disk into data."""

    def __init__(self, *, benchmark_root: Path = DEFAULT_BENCHMARK,
                 results_root: Path = RESULTS) -> None:
        self.benchmark_root = Path(benchmark_root).resolve()
        self.results_root = Path(results_root).resolve()
        self.jobs_root = self.results_root / "jobs"

    # -- the catalogue ------------------------------------------------

    def list_tasks(self, **filters: Any) -> list[dict[str, Any]]:
        """Every task at the pinned revision, optionally filtered."""
        metadata = validate_benchmark(self.benchmark_root)
        rows = [{"task_id": task_id, **details}
                for task_id, details in sorted(metadata["tasks"].items())]
        for key, wanted in filters.items():
            if wanted is None:
                continue
            wanted = {wanted} if isinstance(wanted, str) else set(wanted)
            rows = [row for row in rows if row.get(key) in wanted]
        return rows

    # -- planning -----------------------------------------------------

    def plan(self, spec: JobSpec, *, job_id: str | None = None) -> Job:
        """Resolve a spec into a concrete trial list. No containers, no cost."""
        spec.validate()
        metadata = validate_benchmark(self.benchmark_root)
        tasks = choose_tasks(self._selector(spec.tasks), metadata["tasks"])
        job_id = job_id or self._new_job_id(spec)
        folder = self.jobs_root / job_id
        if (folder / "job.json").exists():
            raise FileExistsError(
                f"job {job_id} already exists; run or inspect it instead: {folder}")
        runs = [f"{job_id}-r{index}" for index in range(1, spec.repeats + 1)]
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1, "job_id": job_id, "created_at": time.time(),
            "spec": spec.to_dict(), "tasks": tasks, "runs": runs,
            "state": "planned", "trial_count": len(tasks) * spec.repeats,
            # Resolved beside the spec so a job file read months later still
            # says what "bench" and that model meant at the time.
            "resolved": {"model_spec": MODELS.get(spec.model),
                         "profile_spec": PROFILES.get(spec.profile),
                         "benchmark_commit": metadata["commit"]},
            "updated_at": time.time(),
        }
        self._write(folder / "job.json", payload)
        return self._job(payload)

    # -- execution ----------------------------------------------------

    def run(self, job_id: str, *, execute: bool = True,
            keep_container: bool = False) -> Job:
        """Run every outstanding replicate. Safe to call again after a pause.

        Each replicate is a subprocess so a crash in one cannot take the job
        with it, and so the launcher's existing exit-code contract stays the
        interface. A replicate whose run directory already exists is resumed,
        which is what makes a usage limit a pause rather than a loss: the
        launcher's own conflict guard refuses to resume under a changed
        configuration, so resumption can never quietly mix two setups.
        """
        payload = self._read_job(job_id)
        spec = JobSpec(**payload["spec"])
        payload["state"] = "running"
        payload["updated_at"] = time.time()
        self._write(self.jobs_root / job_id / "job.json", payload)

        for run_id in payload["runs"]:
            run_dir = self.results_root / run_id
            if self._run_finished(run_dir, payload["tasks"]):
                continue
            command = [
                sys.executable, str(ROOT / "run_harness_bench.py"),
                "--benchmark-root", str(self.benchmark_root),
                "--env-file", spec.env_file, "--image", spec.image,
                "--mode", spec.mode, "--profile", spec.profile,
                "--model", spec.model, "--judge", spec.judge,
            ]
            for task_id in payload["tasks"]:
                command.extend(("--task", task_id))
            if (run_dir / "run.json").exists():
                command.extend(("--resume", str(run_dir), "--retry-failed"))
            else:
                command.extend(("--run-id", run_id))
            if spec.concurrency > 1:
                command.extend(("--concurrency", str(spec.concurrency)))
            if keep_container:
                command.append("--keep-container")
            if execute:
                command.append("--execute")

            completed = subprocess.run(command, cwd=ROOT, check=False)
            if completed.returncode in PAUSE_EXITS:
                payload["state"] = "paused"
                payload["paused_reason"] = PAUSE_EXITS[completed.returncode]
                payload["paused_at_run"] = run_id
                payload["updated_at"] = time.time()
                self._write(self.jobs_root / job_id / "job.json", payload)
                print("\nJob " + job_id + " paused ("
                      + payload["paused_reason"] + "). Resume with: "
                      + f"python harness_bench_api.py run {job_id}", flush=True)
                return self._job(payload)
            # Any other non-zero code means some task failed but the replicate
            # itself ran to the end. Those failures are already scored as
            # zeros; stopping here would confuse "the agent failed" with "the
            # benchmark broke".
            payload.pop("paused_reason", None)
            payload.pop("paused_at_run", None)

        payload["state"] = "complete" if all(
            self._run_finished(self.results_root / run_id, payload["tasks"])
            for run_id in payload["runs"]) else "incomplete"
        payload["updated_at"] = time.time()
        self._write(self.jobs_root / job_id / "job.json", payload)
        return self._job(payload)

    # -- reading back -------------------------------------------------

    def status(self, job_id: str) -> dict[str, Any]:
        """Per-trial state, read from the run directories rather than cached."""
        payload = self._read_job(job_id)
        trials = []
        for run_id in payload["runs"]:
            for task_id in payload["tasks"]:
                status = read_json(
                    self.results_root / run_id / "tasks" / task_id / "status.json") or {}
                trials.append({
                    "trial_id": f"{run_id}/{task_id}", "run_id": run_id,
                    "task_id": task_id, "state": status.get("state", "pending"),
                    "outcome_score": status.get("outcome_score"),
                    "score": task_score(status) if status else None,
                    "elapsed_sec": status.get("elapsed_sec"),
                })
        done = [row for row in trials if row["state"] == "complete"]
        return {
            "job_id": job_id, "state": payload.get("state"),
            "paused_reason": payload.get("paused_reason"),
            "trial_count": len(trials), "completed": len(done),
            "remaining": len(trials) - len(done),
            "trials": trials,
        }

    def dataset(self, job_ids: list[str] | None = None, *,
                output: Path | None = None,
                with_events: bool = False) -> dict[str, Any]:
        """Refresh the SQLite database from run directories on disk.

        Defaults to *every* run, because combining sessions into one score is
        the normal case: the suite gets run in pieces as usage allows.
        """
        if job_ids:
            run_dirs = []
            for job_id in job_ids:
                payload = self._read_job(job_id)
                run_dirs += [self.results_root / run_id for run_id in payload["runs"]]
        else:
            run_dirs = sorted(path for path in self.results_root.iterdir()
                              if path.is_dir() and (path / "run.json").exists())
        run_dirs = [path for path in run_dirs if (path / "run.json").exists()]
        if not run_dirs:
            raise FileNotFoundError("no Harness-Bench runs found on disk")
        return export_runs(run_dirs, output or self.results_root,
                           with_events=with_events,
                           # Pruning is only meaningful over the whole corpus;
                           # narrowing to some jobs must not delete the rest.
                           prune=not job_ids)

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_root.is_dir():
            return []
        rows = []
        for folder in sorted(self.jobs_root.iterdir()):
            payload = read_json(folder / "job.json")
            if not payload:
                continue
            spec = payload.get("spec") or {}
            rows.append({
                "job_id": payload.get("job_id"), "state": payload.get("state"),
                "trial_count": payload.get("trial_count"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "paused_reason": payload.get("paused_reason"),
                "model": spec.get("model"), "profile": spec.get("profile"),
                "mode": spec.get("mode"), "repeats": spec.get("repeats"),
                # ``or 1`` rather than the bare value: jobs planned before this
                # field existed have no key, and a blank column would read as
                # unknown when the answer is definitely one.
                "concurrency": spec.get("concurrency") or 1,
                "notes": spec.get("notes"),
            })
        return rows

    # -- internals ----------------------------------------------------

    @staticmethod
    def _selector(selector: dict[str, Any]) -> argparse.Namespace:
        """Feed the job's selector through the launcher's own resolver.

        Reused rather than reimplemented for the same reason ``task_score`` is
        shared: two resolvers that disagree about what ``--difficulty easy``
        means produce a job whose planned task list does not match the tasks
        the launcher then runs, and nothing would report the mismatch.
        """
        return argparse.Namespace(
            task=list(selector.get("ids") or []) or None,
            all=bool(selector.get("all")),
            difficulty=list(selector.get("difficulty") or []) or None,
            task_class=list(selector.get("class") or []) or None,
            exclude=list(selector.get("exclude") or []) or None,
            pilot=bool(selector.get("pilot")),
            smoke=bool(selector.get("smoke")),
        )

    def _run_finished(self, run_dir: Path, tasks: list[str]) -> bool:
        return all(
            (read_json(run_dir / "tasks" / task_id / "status.json") or {}).get("state")
            == "complete" for task_id in tasks)

    def _read_job(self, job_id: str) -> dict[str, Any]:
        payload = read_json(self.jobs_root / job_id / "job.json")
        if not payload:
            raise FileNotFoundError(f"no such job: {job_id}")
        return payload

    def _job(self, payload: dict[str, Any]) -> Job:
        detail = {key: payload[key] for key in
                  ("created_at", "updated_at", "resolved", "paused_reason")
                  if key in payload}
        return Job(job_id=payload["job_id"], spec=JobSpec(**payload["spec"]),
                   tasks=payload["tasks"], runs=payload["runs"],
                   state=payload["state"],
                   path=str(self.jobs_root / payload["job_id"]), detail=detail)

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    @staticmethod
    def _new_job_id(spec: JobSpec) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return f"{stamp}-{spec.profile}-{spec.mode}-{uuid.uuid4().hex[:6]}"


def main(argv: list[str] | None = None) -> int:
    """JSON on stdout, for shells and for languages that do not import Python."""
    # Task titles carry non-ASCII punctuation, and the default Windows console
    # codec cannot encode it -- the CLI would die printing a perfectly good
    # result.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Harness-Bench job runner")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("tasks", help="the pinned task catalogue")
    subparsers.add_parser("jobs", help="every job planned so far")

    new = subparsers.add_parser("new", help="plan a job without running it")
    new.add_argument("--model", required=True)
    new.add_argument("--profile", default="bench", choices=sorted(PROFILES))
    new.add_argument("--mode", default="yolo", choices=MODES)
    new.add_argument("--concurrency", type=int, default=1, metavar="N",
                     help="run N tasks at once (default 1)")
    new.add_argument("--judge", default="none", choices=["none", *sorted(MODELS)],
                     help="model that grades process and security; 'none' pins both to 1.0")
    new.add_argument("--repeats", type=int, default=1)
    new.add_argument("--task", action="append", help="exact task id; repeatable")
    new.add_argument("--difficulty", action="append")
    new.add_argument("--task-class", action="append")
    new.add_argument("--exclude", action="append")
    new.add_argument("--all", action="store_true")
    new.add_argument("--pilot", action="store_true")
    new.add_argument("--smoke", action="store_true")
    new.add_argument("--env-file", default="bench.env")
    new.add_argument("--notes", default="")
    new.add_argument("--job-id")

    run = subparsers.add_parser("run", help="run or resume a planned job")
    run.add_argument("job_id")
    run.add_argument("--dry-run", action="store_true",
                     help="schedule without authorizing model calls")
    run.add_argument("--keep-container", action="store_true")

    status = subparsers.add_parser("status", help="per-trial state of one job")
    status.add_argument("job_id")

    export = subparsers.add_parser("export", help="rebuild the SQLite dataset")
    export.add_argument("--job", action="append", help="limit to these jobs; repeatable")
    export.add_argument("--output")
    export.add_argument("--with-events", action="store_true",
                        help="also load every raw event row (large)")

    options = parser.parse_args(argv)
    api = HarnessBenchAPI()

    if options.action == "tasks":
        result: Any = api.list_tasks()
    elif options.action == "jobs":
        result = api.list_jobs()
    elif options.action == "new":
        selector = {"ids": options.task, "difficulty": options.difficulty,
                    "class": options.task_class, "exclude": options.exclude,
                    "all": options.all, "pilot": options.pilot,
                    "smoke": options.smoke}
        result = api.plan(JobSpec(
            model=options.model, profile=options.profile, mode=options.mode,
            judge=options.judge,
            repeats=options.repeats, concurrency=options.concurrency,
            env_file=options.env_file,
            notes=options.notes,
            tasks={key: value for key, value in selector.items() if value},
        ), job_id=options.job_id).to_dict()
    elif options.action == "run":
        result = api.run(options.job_id, execute=not options.dry_run,
                         keep_container=options.keep_container).to_dict()
    elif options.action == "status":
        result = api.status(options.job_id)
    else:
        result = api.dataset(options.job, with_events=options.with_events,
                             output=Path(options.output) if options.output else None)

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
