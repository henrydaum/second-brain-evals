"""Run pinned Harness-Bench tasks against Second Brain, one fresh container each.

Safe defaults do not call a model. Use ``--validate`` to inspect the release,
``--build-image`` to prepare the runtime, and add ``--execute`` only when a
paid MiniMax (or other provider) run is intended.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import posixpath
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml



ROOT = Path(__file__).resolve().parent
EVAL_DIR = ROOT / "evals" / "harness_bench"
LOCK = json.loads((EVAL_DIR / "benchmark.lock.json").read_text(encoding="utf-8"))
SELECTIONS = json.loads((EVAL_DIR / "pilot.json").read_text(encoding="utf-8"))
#: Plugin sets a job can ask for. ``seed`` is baked into the image by
#: ``build_template.py``; ``runtime`` is the delta ``entrypoint.py`` applies
#: when the container starts. A job names a ``runtime`` profile.
PROFILES = json.loads((ROOT / "profiles.json").read_text(encoding="utf-8"))["runtime"]
#: Endpoint, backend and pricing per model, so ``--model`` is enough to switch
#: providers and so cost can be computed later from the same source of truth.
MODELS = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))["models"]
DEFAULT_BENCHMARK = ROOT / "build" / "harness-bench-src"
DEFAULT_IMAGE = "second-brain:harness-bench"
RESULTS = ROOT / "results" / "harness-bench"
#: The tool set the **default ``bench`` profile** is expected to expose, used
#: by ``--self-test`` to prove the profile actually took effect.
#:
#: This is a pin, not a description of a run. Every other consumer derives the
#: tool list from the template manifest and the job's profile delta, because a
#: run under ``--profile no-script`` legitimately has fewer tools and a
#: constant would quietly misreport it. Only the self-test may compare against
#: this, and only because the self-test always runs the default profile.
BENCHMARK_TOOLS = {
    "edit_file", "glob", "grep", "read_file", "run_command", "run_script",
    "schedule_subagent", "spawn_subagent", "sql_query", "validate", "web_search",
}
CONTAINER_BENCH_ROOT = "/work/harnessbench"
# run_command deliberately accepts cwd only under Second Brain's application
# or data roots. Keeping official task sandboxes in the agent data workspace
# makes the real workspace reachable without weakening that production rule.
CONTAINER_WORK_ROOT = "/data/Second Brain/workspace/harnessbench-sandboxes"
#: Seconds reserved out of a task's official timeout for the driver to end its
#: turn and write its result bundle. See ``stage_benchmark``.
DRIVER_COLLECT_MARGIN_S = 90
PROVIDER_PATTERNS = (
    "usage limit reached",
    "quota exceeded",
    "insufficient_quota",
    "insufficient credits",
    "rate_limit_error",
    "error code: 2056",
)


class ProviderUnavailableError(RuntimeError):
    """The configured model cannot accept a paid benchmark call right now."""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--smoke", action="store_true", help="two-task file + multi-round session smoke")
    selection.add_argument("--pilot", action="store_true", help="eight-task category-stratified pilot")
    selection.add_argument("--task", action="append", help="exact task id; repeatable")
    selection.add_argument("--all", action="store_true", help="every task at the pinned revision")
    parser.add_argument("--difficulty", action="append",
                        help="easy|medium|medium-hard|hard|unsorted; repeatable, combines with --task-class")
    parser.add_argument("--task-class", action="append", help="published category; repeatable")
    parser.add_argument("--exclude", action="append", help="task id to drop from the selection; repeatable")
    parser.add_argument("--mode", choices=("yolo", "lockdown", "mediated"), default="yolo")
    parser.add_argument("--profile", default="bench", choices=sorted(PROFILES),
                        help="plugin set, applied at container start (see profiles.json)")
    parser.add_argument("--model", help="model id from models.json; overrides SB_LLM_MODEL")
    parser.add_argument("--benchmark-root", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--fetch-benchmark", action="store_true", help="clone/fetch the exact pinned upstream revision")
    parser.add_argument("--env-file", default="bench.env")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--build-image", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="run official demo oracle and mode reuse without a model call")
    parser.add_argument("--execute", action="store_true", help="authorize provider-backed task execution")
    parser.add_argument("--skip-provider-check", action="store_true")
    parser.add_argument("--allow-stale-image", action="store_true",
                        help="run even though the image predates the requested profile")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", metavar="RUN_DIR")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--keep-container", action="store_true")
    options = parser.parse_args(argv)

    benchmark = Path(options.benchmark_root).resolve()
    if options.fetch_benchmark:
        fetch_benchmark(benchmark)
    metadata = validate_benchmark(benchmark)
    if options.validate:
        print(json.dumps(metadata, indent=2, ensure_ascii=False))
        if not options.build_image and not options.self_test and not options.execute:
            return 0

    if options.build_image:
        build_image(options.image)
        if not options.self_test and not options.execute:
            return 0

    if options.self_test:
        self_test(benchmark, options.image)
        if not options.execute:
            return 0

    tasks = choose_tasks(options, metadata["tasks"])

    # Checked before the task list is even priced: a stale image is cheapest
    # to notice now, and most expensive to notice after a full suite has run
    # under a label that was never true.
    freshness = image_freshness(options.profile)
    for line in freshness["warn"]:
        print(f"warning: {line}", file=sys.stderr)
    if freshness["fatal"] and not options.allow_stale_image:
        for line in freshness["fatal"]:
            print(f"error: {line}", file=sys.stderr)
        print("\nRebuild with:\n"
              "  python build_template.py --profile bench\n"
              "  python run_harness_bench.py --build-image\n"
              "or pass --allow-stale-image to run anyway.", file=sys.stderr)
        return 2

    if not options.execute:
        print("No model calls made. Add --execute to run: " + ", ".join(tasks))
        return 0

    env_file = Path(options.env_file).resolve()
    if not env_file.is_file():
        parser.error(f"env file does not exist: {env_file}")
    env_values = read_env(env_file)
    # ``--model`` names an entry in models.json, whose endpoint and backend
    # then override the env file. The API key stays in the env file: it is the
    # one setting that must not live in a committed, publishable description.
    model = options.model or env_values.get("SB_LLM_MODEL")
    if not model:
        parser.error("no model: pass --model or set SB_LLM_MODEL in the env file")
    if options.model and options.model not in MODELS:
        parser.error(f"unknown model {options.model!r}; models.json knows: "
                     + ", ".join(sorted(MODELS)))
    spec = MODELS.get(model) or {}
    env_overrides = {
        "SB_LLM_MODEL": model,
        "SB_LLM_ENDPOINT": spec.get("endpoint") or env_values.get("SB_LLM_ENDPOINT", ""),
        "SB_LLM_BACKEND": spec.get("backend") or env_values.get("SB_LLM_BACKEND", ""),
        "SB_PROFILE": options.profile,
        "SB_ADD_PACKAGES": ",".join(PROFILES[options.profile].get("add") or []),
        "SB_REMOVE_PACKAGES": ",".join(PROFILES[options.profile].get("remove") or []),
    }
    if spec.get("context_size"):
        env_overrides["SB_LLM_CONTEXT"] = str(spec["context_size"])
    # Preflight has to test the endpoint the containers will actually use, not
    # whatever the env file happened to name.
    preflight_values = dict(env_values)
    preflight_values.update({k: v for k, v in env_overrides.items() if v})
    if not options.skip_provider_check:
        try:
            provider_preflight(model, preflight_values)
        except ProviderUnavailableError as exc:
            print(f"Provider unavailable; no benchmark task started: {exc}", file=sys.stderr)
            return 75

    image_id = inspect_image(options.image)
    run_dir, run = open_run(options, tasks, model, metadata, image_id)
    print(f"Harness-Bench run {run['run_id']}: {len(tasks)} tasks, "
          f"{options.mode}, profile {options.profile}, {model}")
    print(f"Results: {run_dir}")
    write_json(run_dir / "run.json", run)

    exit_code = 0
    for index, task_id in enumerate(tasks, 1):
        task_dir = run_dir / "tasks" / task_id
        previous = read_json(task_dir / "status.json") or {}
        if previous.get("state") == "complete":
            print(f"[{index}/{len(tasks)}] {task_id}: already complete")
            continue
        if previous and not options.retry_failed and previous.get("state") not in ("pending", "interrupted"):
            print(f"[{index}/{len(tasks)}] {task_id}: {previous.get('state')} (use --retry-failed)")
            continue
        try:
            result = run_one_task(
                task_id=task_id,
                task_dir=task_dir,
                benchmark=benchmark,
                env_file=env_file,
                image=options.image,
                mode=options.mode,
                model=model,
                keep_container=options.keep_container,
                position=f"{index}/{len(tasks)}",
                env_overrides=env_overrides,
            )
        except KeyboardInterrupt:
            # A *second* Ctrl+C, landing while the first one's cleanup was
            # still copying evidence out of the container. Take it as "stop
            # now", but still write the summary on the way out so the run
            # directory is resumable rather than half-described.
            print(f"\nInterrupted during cleanup; stopping. "
                  f"Resume with: --resume {run_dir}", flush=True)
            exit_code = 130
            break
        write_summary(run_dir, tasks)
        if result.get("state") == "provider_unavailable":
            print("Provider quota/credit failure detected; stopping before another task is scheduled.")
            exit_code = 75
            break
        if result.get("state") == "interrupted":
            # Ctrl+C is caught inside ``run_one_task`` so the active task's
            # container is still torn down and its partial evidence still
            # collected. That made the *interrupt* clean and the *run* not:
            # without this branch the loop simply started the next container
            # and the next paid task, so the only way out was to keep
            # pressing Ctrl+C and hope one landed between two ``try`` blocks.
            print("Interrupted; stopping. Resume with: "
                  f"--resume {run_dir}", flush=True)
            exit_code = 130
            break
        if result.get("state") not in ("complete",):
            exit_code = 1

    summary = write_summary(run_dir, tasks)
    print_summary(summary)
    return exit_code


def validate_benchmark(root: Path) -> dict[str, Any]:
    required = (root / "src" / "harnessbench", root / "tasks", root / "grading")
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Harness-Bench checkout is incomplete: " + ", ".join(missing))
    commit = git(root, "rev-parse", "HEAD")
    if commit != LOCK["commit"]:
        raise RuntimeError(
            f"Harness-Bench revision is {commit}, expected pinned {LOCK['commit']}. "
            "Update benchmark.lock.json deliberately before mixing results."
        )
    dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError("Harness-Bench checkout has modified tracked files; refusing a non-reproducible run:\n" + dirty)
    config_result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "core.autocrlf"],
        capture_output=True, text=True, check=False,
    )
    autocrlf = config_result.stdout.strip()
    if autocrlf.lower() not in {"false", "input"}:
        raise RuntimeError(
            "Harness-Bench must be materialized with core.autocrlf=false. "
            "Its fixture oracles hash canonical bytes, so CRLF conversion creates false integrity failures. "
            "Run --fetch-benchmark to repair the ignored benchmark checkout."
        )
    tasks: dict[str, dict[str, Any]] = {}
    for manifest in sorted((root / "tasks").glob("*/task.yaml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        task_id = str(data.get("task_id") or manifest.parent.name)
        tasks[task_id] = {
            "title": str(data.get("title") or task_id),
            "class": str(data.get("class") or "unspecified"),
            "difficulty": str(data.get("difficulty") or "unspecified"),
            "timeout_sec": int(data.get("timeout_sec") or 600),
            "rounds": len(data.get("prompt_files") or []) or 1,
        }
    if len(tasks) != int(LOCK["task_count"]):
        raise RuntimeError(f"expected {LOCK['task_count']} tasks at pinned revision; found {len(tasks)}")
    return {
        "repository": LOCK["repository"],
        "commit": commit,
        "task_count": len(tasks),
        "license_file_present": any((root / name).exists() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")),
        "tasks": tasks,
        "smoke": SELECTIONS["smoke"],
        "pilot": SELECTIONS["pilot"],
    }


def fetch_benchmark(root: Path) -> None:
    """Materialize the external benchmark without vendoring its unlicensed tree."""
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", LOCK["repository"], str(root)], check=True)
    elif not (root / ".git").is_dir():
        raise RuntimeError(f"benchmark root exists but is not a Git checkout: {root}")
    current = git(root, "rev-parse", "HEAD")
    if current != LOCK["commit"]:
        subprocess.run(["git", "-C", str(root), "fetch", "origin", LOCK["commit"]], check=True)
        subprocess.run(["git", "-C", str(root), "checkout", "--detach", LOCK["commit"]], check=True)
    # Harness-Bench embeds canonical MD5s for fixture-integrity checks. On
    # Windows, a global core.autocrlf=true silently rewrites those fixtures
    # while leaving Git's status clean, producing false agent penalties.
    subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(root), "checkout-index", "--all", "--force"], check=True)


def choose_tasks(options: argparse.Namespace, available: dict[str, Any]) -> list[str]:
    """Resolve a task selection from ids, difficulty, class, or the whole suite.

    Difficulty is taken verbatim from each ``task.yaml`` and is **not** a tidy
    three-bucket scheme. At the pinned revision the values are ``hard`` (42),
    ``medium`` (30), ``unspecified`` (24, the tasks that declare no difficulty
    at all), ``easy`` (7) and ``medium-hard`` (3) -- so a filter for "not hard"
    that forgets ``medium-hard`` silently drops three tasks, and one for "easy
    plus medium" quietly covers 37 of 106. Unknown filter values raise rather
    than matching nothing, because a typo that yields an empty run looks
    exactly like a finished one.
    """
    chosen = list(options.task or [])
    filtered = bool(options.difficulty or options.task_class)
    if filtered:
        wanted_difficulty = set(options.difficulty or [])
        wanted_class = set(options.task_class or [])
        known_difficulty = {row["difficulty"] for row in available.values()}
        known_class = {row["class"] for row in available.values()}
        for label, wanted, known in (("difficulty", wanted_difficulty, known_difficulty),
                                     ("class", wanted_class, known_class)):
            unknown = sorted(wanted - known)
            if unknown:
                raise RuntimeError(
                    f"unknown {label}: {', '.join(unknown)}. "
                    f"Known values: {', '.join(sorted(known))}")
        chosen += [
            task_id for task_id, row in sorted(available.items())
            if (not wanted_difficulty or row["difficulty"] in wanted_difficulty)
            and (not wanted_class or row["class"] in wanted_class)
        ]
    elif options.all:
        chosen += sorted(available)
    elif not chosen:
        chosen = list(SELECTIONS["pilot"] if options.pilot else SELECTIONS["smoke"])

    unknown = [task for task in chosen if task not in available]
    if unknown:
        raise RuntimeError("unknown Harness-Bench task(s): " + ", ".join(unknown))
    excluded = set(options.exclude or [])
    resolved = [task for task in dict.fromkeys(chosen) if task not in excluded]
    if not resolved:
        raise RuntimeError("task selection resolved to nothing")
    return resolved


def kernel_repo() -> Path:
    """The Second Brain checkout the image is supposed to be built from."""
    configured = os.environ.get("SECOND_BRAIN_REPO")
    if configured:
        return Path(configured)
    return ROOT.parent / "Second Brain"


def image_freshness(profile: str) -> dict[str, list[str]]:
    """Compare what the image was built from with what is on disk now.

    **A stale image fails silently and expensively, which is why this exists.**
    The image carries its own kernel, its own copy of the store packages, and
    its own ``entrypoint.py``. Editing any of those in the checkout changes
    nothing until the image is rebuilt, and every symptom of forgetting looks
    like a different bug:

    * a kernel without the widened telemetry reports no output tokens, so cost
      silently halves;
    * an ``entrypoint.py`` without profile support ignores
      ``SB_REMOVE_PACKAGES`` entirely, so a run *labelled* ``no-script`` runs
      with ``run_script`` and the label is simply wrong;
    * a template built before ``tool_names`` leaves ``visible_tools`` null.

    Returns ``{"fatal": [...], "warn": [...]}``. A wrong label is fatal
    because the resulting data is not merely incomplete, it is mislabelled,
    and nothing downstream can tell. Missing telemetry is a warning: the run
    is honest about what it recorded, just less useful.
    """
    manifest = read_json(ROOT / "template" / "template_manifest.json") or {}
    fatal: list[str] = []
    warn: list[str] = []

    # The image predates runtime profiles if its template was built before the
    # builder started recording installed files. Requesting a non-default
    # profile against it produces a run whose profile column is a lie.
    supports_profiles = "tool_names" in manifest
    wants_delta = bool((PROFILES.get(profile) or {}).get("add")
                       or (PROFILES.get(profile) or {}).get("remove"))
    if wants_delta and not supports_profiles:
        fatal.append(
            f"--profile {profile} needs an image built after runtime profiles "
            "existed, but template/template_manifest.json records no "
            "'tool_names'. The container would ignore the profile and run the "
            "seed's plugins while the results claimed otherwise.")
    elif not supports_profiles:
        warn.append("template predates 'tool_names'; visible_tools will be null.")

    repo = kernel_repo()
    if (repo / ".git").exists():
        for label, ref, built in (("kernel", "HEAD", manifest.get("kernel_commit")),
                                  ("store", "origin/store", manifest.get("store_commit"))):
            try:
                current = git(repo, "rev-parse", ref)
            except Exception:                                # noqa: BLE001
                continue
            if built and current and built != current:
                warn.append(
                    f"{label} moved since the image was built: image has "
                    f"{built[:12]}, checkout has {current[:12]}.")
    return {"fatal": fatal, "warn": warn}


def build_image(image: str) -> None:
    command = [docker_exe(), "build", "-f", str(ROOT / "Dockerfile.bench"), "-t", image, str(ROOT)]
    print("Building:", subprocess.list2cmdline(command), flush=True)
    env = dict(os.environ)
    env["PATH"] = str(Path(command[0]).resolve().parent) + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode:
        raise RuntimeError(f"benchmark image build failed ({completed.returncode})")


def self_test(benchmark: Path, image: str) -> None:
    """Exercise the upstream runner, oracle, Docker runtime, and mode reuse free."""
    inspect_image(image)
    name = safe_container_name("sb-hb-self-test-" + uuid.uuid4().hex[:6])
    try:
        with tempfile.TemporaryDirectory(prefix="sb-harnessbench-selftest-") as temp:
            temp_root = Path(temp)
            stage = temp_root / "harnessbench"
            stage_benchmark(benchmark, stage, "001-file", "yolo", "openai/fake")
            harness_config = read_json(stage / "config" / "harness.json")
            harness_config["models"]["demo"] = {"adapter": "demo"}
            write_json(stage / "config" / "harness.json", harness_config)
            env_file = temp_root / "self-test.env"
            env_file.write_text(
                "SB_LLM_API_KEY=fake\n"
                "SB_LLM_MODEL=openai/fake\n"
                "SB_LLM_ENDPOINT=http://127.0.0.1:9999/v1\n"
                "SB_HTTP_TOKEN=probe-token\n",
                encoding="utf-8",
            )
            run_container(name, image, env_file)
            copy_into(name, stage, "/work/harnessbench")
            copied = docker("cp", str(ROOT / "probes" / "probe_harness_modes.py"), f"{name}:/work/probe_harness_modes.py")
            if copied.returncode:
                raise RuntimeError(copied.stderr or "could not copy mode probe")
            mode_probe = docker("exec", name, "python", "/work/probe_harness_modes.py")
            if mode_probe.returncode:
                raise RuntimeError("mode/session probe failed:\n" + mode_probe.stdout + mode_probe.stderr)
            fake_copy = docker("cp", str(ROOT / "probes" / "fake_openai_server.py"), f"{name}:/work/fake_openai_server.py")
            if fake_copy.returncode:
                raise RuntimeError(fake_copy.stderr or "could not copy fake provider")
            fake_start = docker("exec", "-d", name, "python", "/work/fake_openai_server.py")
            if fake_start.returncode:
                raise RuntimeError(fake_start.stderr or "could not start fake provider")
            time.sleep(0.25)
            protect_benchmark_assets(name)
            command = (
                "PYTHONPATH=/work/harnessbench/src "
                "HARNESSBENCH_APP_CONFIG=/work/harnessbench/config/app.json "
                "HARNESSBENCH_HARNESS_CONFIG=/work/harnessbench/config/harness.json "
                "HARNESSBENCH_SKIP_PROCESS_GRADE=1 "
                "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1 "
                "python -m harnessbench.cli run-task --task 001-file --harness demo --mode demo"
            )
            demo = docker("exec", "-u", "0", name, "sh", "-c", command)
            if demo.returncode:
                raise RuntimeError("official demo run failed:\n" + demo.stdout + demo.stderr)
            result = docker(
                "exec", name, "cat",
                "/work/harnessbench/results/demo/unknown-api/001-file.json",
            )
            payload = json.loads(result.stdout)
            score = (payload.get("oracle_result") or {}).get("outcome_score")
            if score != 1.0:
                raise RuntimeError(f"official demo oracle returned {score!r}, expected 1.0")
            agent_command = command.replace("--harness demo --mode demo", "--harness second-brain --mode live")
            agent = docker("exec", "-u", "0", name, "sh", "-c", agent_command)
            if agent.returncode:
                raise RuntimeError("Second Brain fake-provider task failed:\n" + agent.stdout + agent.stderr)
            found = docker("exec", name, "sh", "-c", "find /work/harnessbench/results -name 001-file.json -path '*second-brain*' -print -quit")
            if not found.stdout.strip():
                raise RuntimeError("Second Brain official result was not written")
            agent_payload = json.loads(docker("exec", name, "cat", found.stdout.strip()).stdout)
            agent_score = (agent_payload.get("oracle_result") or {}).get("outcome_score")
            provider_log = docker("exec", name, "cat", "/work/fake-provider.jsonl", check=False).stdout
            if agent_score != 1.0:
                agent_workspace = str((agent_payload.get("oracle_result") or {}).get("workspace") or "")
                sandbox_root = posixpath.dirname(agent_workspace) if agent_workspace else ""
                driver_result = docker(
                    "exec", name, "cat", f"{sandbox_root}/second-brain/round-01/result.json",
                    check=False,
                ).stdout if sandbox_root else ""
                app_log = docker(
                    "exec", name, "sh", "-c", "tail -n 160 '/data/Second Brain/app.log'",
                    check=False,
                ).stdout
                sandbox_files = docker(
                    "exec", name, "find", sandbox_root, "-maxdepth", "5", "-type", "f", "-print",
                    check=False,
                ).stdout if sandbox_root else ""
                diagnostic = json.dumps({
                    "oracle_result": agent_payload.get("oracle_result"),
                    "adapter_result": agent_payload.get("adapter_result"),
                    "driver_result": read_json_text(driver_result),
                    "fake_provider_log": provider_log,
                    "sandbox_files": sandbox_files,
                    "app_log_tail": app_log,
                }, indent=2, ensure_ascii=False, default=str)
                raise RuntimeError(
                    f"Second Brain fake-provider oracle returned {agent_score!r}, expected 1.0:\n{diagnostic[-12000:]}"
                )
            calls = docker("exec", name, "sh", "-c", "wc -l < /work/fake-provider.jsonl")
            if int(calls.stdout.strip() or 0) < 2:
                raise RuntimeError("fake provider did not observe the expected tool loop")
            provider_rows = [json.loads(line) for line in provider_log.splitlines() if line.strip()]
            observed_tools = set(provider_rows[0].get("tools") or []) if provider_rows else set()
            if observed_tools != BENCHMARK_TOOLS:
                raise RuntimeError(
                    "Essentials-minus-interactive tool profile was not enforced: "
                    f"expected={sorted(BENCHMARK_TOOLS)!r} observed={sorted(observed_tools)!r}"
                )
            print("Self-test passed: pinned release, generalized image boot, mode reuse, official runner, actual Second Brain tool loop, live workspace, and oracle score 1.0; external model calls=0.")
    finally:
        docker("rm", "-f", name, check=False)


def open_run(options, tasks, model, metadata, image_id):
    if options.resume:
        run_dir = Path(options.resume).resolve()
        run = read_json(run_dir / "run.json")
        if not run:
            raise RuntimeError(f"resume metadata is missing: {run_dir / 'run.json'}")
        expected = {"mode": options.mode, "model": model, "profile": options.profile,
                    "benchmark_commit": metadata["commit"], "image_id": image_id}
        conflicts = {key: (run.get(key), value) for key, value in expected.items() if run.get(key) != value}
        if conflicts:
            raise RuntimeError("refusing to mix configurations while resuming: " + repr(conflicts))
        old_tasks = list(run.get("tasks") or [])
        if tasks != old_tasks:
            raise RuntimeError(f"resume task selection differs: {tasks!r} != {old_tasks!r}")
        return run_dir, run

    run_id = options.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{options.mode}"
    run_dir = RESULTS / run_id
    if (run_dir / "run.json").exists():
        raise RuntimeError(f"run already exists; use --resume {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    template = read_json(ROOT / "template" / "template_manifest.json") or {}
    profile = PROFILES[options.profile]
    # **Derived, never asserted.** ``visible_tools`` used to be a module
    # constant, which was harmless only while the plugin set never changed.
    # The moment a job varies plugins -- the reason this file grew a
    # ``--profile`` flag -- a hardcoded list makes every run.json claim a tool
    # set it did not have. The seed's real contents come from the template
    # manifest; the per-task ground truth is ``live/profile.json``, written by
    # the entrypoint after the delta is applied.
    seed_tools = set(template.get("tool_names") or [])
    expected_tools = sorted(
        (seed_tools | {stem.removeprefix("tool_") for stem in profile.get("add") or []})
        - {stem.removeprefix("tool_") for stem in profile.get("remove") or []}
    ) if seed_tools else None
    run = {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": time.time(),
        "mode": options.mode,
        "model": model,
        "model_spec": MODELS.get(model),
        "profile": options.profile,
        "profile_spec": profile,
        "tool_profile": profile.get("description") or options.profile,
        # ``None`` when the template predates ``tool_names`` -- absent rather
        # than guessed, so nobody reads a stale constant as measurement.
        "visible_tools": expected_tools,
        "tasks": tasks,
        "task_metadata": {task_id: metadata["tasks"][task_id] for task_id in tasks},
        "benchmark_repository": metadata["repository"],
        "benchmark_commit": metadata["commit"],
        "benchmark_license_file_present": metadata["license_file_present"],
        "image": options.image,
        "image_id": image_id,
        "template": template,
        "process_grade": "skipped",
        "oracle_quality_llm": "skipped",
        # Scoring deviations from the pinned release, recorded so a comparison
        # can tell whether two numbers were produced the same way. A task
        # listed here is comparable with another run of *this* harness and not
        # with a published Harness-Bench figure for the same task.
        "oracle_normalizations": sorted(
            task_id for task_id in tasks if task_id in ORACLE_GROUND_TRUTH_REWRITES),
        "reported_metric": "Harness-Bench deterministic completion",
    }
    return run_dir, run


def run_one_task(*, task_id, task_dir, benchmark, env_file, image, mode, model,
                 keep_container, position, env_overrides=None):
    task_dir.mkdir(parents=True, exist_ok=True)
    write_json(task_dir / "problem.json", snapshot_problem(benchmark, task_id))
    status = {"task_id": task_id, "state": "running", "started_at": time.time(),
              "mode": mode, "model": model,
              "profile": (env_overrides or {}).get("SB_PROFILE")}
    write_json(task_dir / "status.json", status)
    container = safe_container_name(f"sb-hb-{task_id}-{uuid.uuid4().hex[:6]}")
    tail = None
    event_thread = None
    returncode = None
    try:
        with tempfile.TemporaryDirectory(prefix="sb-harnessbench-") as temp:
            stage = Path(temp) / "harnessbench"
            stage_benchmark(benchmark, stage, task_id, mode, model)
            run_container(container, image, env_file, env_overrides)
            copy_into(container, stage, "/work/harnessbench")
            protect_benchmark_assets(container)
            tail, event_thread = stream_events(container, task_dir / "events.jsonl")
            print(f"[{position}] {task_id}: running", flush=True)
            command = [
                docker_exe(), "exec", "-u", "0",
                "-e", "PYTHONPATH=/work/harnessbench/src",
                "-e", "PYTHONIOENCODING=utf-8",
                "-e", "PYTHONUTF8=1",
                "-e", "HARNESSBENCH_APP_CONFIG=/work/harnessbench/config/app.json",
                "-e", "HARNESSBENCH_HARNESS_CONFIG=/work/harnessbench/config/harness.json",
                "-e", "HARNESSBENCH_SKIP_PROCESS_GRADE=1",
                "-e", "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1",
                container,
                "python", "-m", "harnessbench.cli", "run-task",
                "--task", task_id, "--harness", "second-brain", "--mode", "live",
            ]
            returncode = run_logged(command, task_dir / "harness.log")
    except KeyboardInterrupt:
        status["state"] = "interrupted"
        status["error"] = "interrupted by user"
    except Exception as exc:  # noqa: BLE001
        status["state"] = "harness_error"
        status["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if tail is not None:
            stop_process(tail)
        if event_thread is not None:
            event_thread.join(timeout=3)
        collect_container(container, task_dir)
        authoritative_events = task_dir / "live" / "events.jsonl"
        if authoritative_events.is_file():
            shutil.copyfile(authoritative_events, task_dir / "events.jsonl")
        logs = container_logs(container)
        (task_dir / "container.log").write_text(logs, encoding="utf-8", errors="replace")
        if not keep_container:
            docker("rm", "-f", container, check=False)

    official = find_official_result(task_dir, task_id)
    provider_error = detect_provider_failure(task_dir)
    # **An official result outranks a provider complaint, and the order here
    # is the whole point.** ``detect_provider_failure`` greps the logs for
    # phrases like ``rate_limit_error``, and a provider client that *retried
    # a 429 and then succeeded* logs exactly that phrase on its way to a
    # finished task. Checking the complaint first threw away a real oracle
    # score, recorded the task as a zero, and stopped the whole run -- on a
    # rate-limited provider, which is to say on the runs where it matters.
    #
    # So a task that produced a result is scored, and the complaint is kept
    # beside it as ``provider_warning`` rather than discarded: a run littered
    # with those is worth knowing about even when every task completed.
    if official:
        status["state"] = "complete"
        status["official_result"] = str(official.relative_to(task_dir)).replace("\\", "/")
        payload = read_json(official) or {}
        status["outcome_score"] = ((payload.get("oracle_result") or {}).get("outcome_score"))
        status["combined_score"] = ((payload.get("scoring") or {}).get("combined_score"))
        status["adapter_ok"] = ((payload.get("adapter_result") or {}).get("ok"))
        status["elapsed_sec"] = payload.get("elapsed_sec")
        if provider_error:
            status["provider_warning"] = provider_error
    elif provider_error:
        status["state"] = "provider_unavailable"
        status["provider_error"] = provider_error
    elif status.get("state") == "running":
        status["state"] = "harness_error"
        status["error"] = f"Harness-Bench exited {returncode} without a result"
    status["finished_at"] = time.time()
    write_json(task_dir / "status.json", status)
    append_event(task_dir / "events.jsonl", {"at": time.time(), "source": "harness", "kind": "task_result", "status": status})
    score = status.get("outcome_score")
    print(f"[{position}] {task_id}: {status['state']} score={score}", flush=True)
    return status


def snapshot_problem(benchmark: Path, task_id: str) -> dict[str, Any]:
    """Copy the official problem text beside the run for live inspection."""
    root = benchmark / "tasks" / task_id
    manifest = yaml.safe_load((root / "task.yaml").read_text(encoding="utf-8")) or {}
    names = list(manifest.get("prompt_files") or [])
    if not names and manifest.get("prompt_file"):
        names = [manifest["prompt_file"]]
    return {
        "task_id": task_id,
        "title": manifest.get("title"),
        "rounds": [
            {"round": index, "file": name,
             "text": (root / name).read_text(encoding="utf-8")}
            for index, name in enumerate(names, 1)
        ],
    }


#: The idiom twelve of the thirteen Knowledge-class oracles use to find their
#: own ``ground_truth.json``: resolve it beside the oracle module. Task 012
#: instead walks up from the *workspace*, which never reaches the task
#: directory -- upstream builds sandboxes at
#: ``<work_root>/<model_id>/<api_slug>/oc-bench-v2-.../workspace``, so
#: ``workspace.parent.parent`` is the api-slug directory in every
#: configuration, ours included. See :func:`normalize_oracle_ground_truth`.
ORACLE_GROUND_TRUTH_IDIOM = "Path(__file__).resolve().parent"
#: Workspace-relative resolutions we rewrite, mapped to what they become. Kept
#: as exact source text so an upstream revision that fixes or moves the line
#: fails the assertion below instead of being silently "fixed" again.
ORACLE_GROUND_TRUTH_REWRITES = {
    "012-doc-synthesis": ("task_dir = w.parent.parent",
                          f"task_dir = {ORACLE_GROUND_TRUTH_IDIOM}"),
}


def normalize_oracle_ground_truth(task_dir: Path, task_id: str) -> None:
    """Make a staged oracle resolve ``ground_truth.json`` beside itself.

    **A missing ground truth does not fail a task, it silently re-weights it.**
    Task 012's oracle reads its expectations through ``Path.exists()`` and
    carries on with ``{}`` when the read misses. The three branches then
    disagree about what an empty expectation means: ``trust_assessment``
    divides by ``len(expected)`` and falls through to ``else 0.0``, while
    ``contradiction_detection`` and ``report_quality`` divide by empty lists
    and default to a full ``1.0``. Every trial scores exactly
    ``0*0.25 + 1*0.35 + 1*0.40 = 0.75`` no matter what the agent wrote, and
    nothing downstream can tell that from three real results.

    The rewrite happens on the staged copy. The pinned checkout under
    ``build/`` keeps the revision recorded in ``benchmark.lock.json``.

    Deviating from upstream scoring is deliberate and is recorded per run by
    :func:`stage_benchmark`, because a task graded this way is **not**
    comparable with a published Harness-Bench figure for the same task -- only
    with another run of this harness.
    """
    rewrite = ORACLE_GROUND_TRUTH_REWRITES.get(task_id)
    if rewrite is None:
        return
    oracle = task_dir / "oracle_grade.py"
    before, after = rewrite
    source = oracle.read_text(encoding="utf-8")
    if before not in source:
        # Either upstream fixed it or the line moved. Both mean this shim is
        # now guessing, and a scoring shim that guesses is worse than none.
        raise RuntimeError(
            f"{task_id}: expected {before!r} in oracle_grade.py to rewrite, but it is "
            "absent. The pinned benchmark revision changed; re-check whether "
            "ORACLE_GROUND_TRUTH_REWRITES is still needed before running.")
    oracle.write_text(source.replace(before, after), encoding="utf-8")


def assert_ground_truth_reachable(task_dir: Path, task_id: str) -> None:
    """Refuse to run when an oracle reads a ground truth that is not there.

    Cheap insurance against the whole failure class rather than the one
    instance of it: an oracle that mentions ``ground_truth.json`` and cannot
    open it beside itself produces scores that look finished and are not.
    """
    oracle = task_dir / "oracle_grade.py"
    if not oracle.is_file():
        return
    source = oracle.read_text(encoding="utf-8")
    if "ground_truth.json" not in source:
        return
    if not (task_dir / "ground_truth.json").is_file():
        raise RuntimeError(
            f"{task_id}: oracle_grade.py reads ground_truth.json but the staged task "
            "directory has no such file. Scoring would silently fall back to empty "
            "expectations and report partial credit the agent did not earn.")


def stage_benchmark(source: Path, dest: Path, task_id: str, mode: str, model: str) -> None:
    shutil.copytree(source / "src", dest / "src")
    shutil.copytree(source / "grading", dest / "grading")
    shutil.copytree(source / "tasks" / task_id, dest / "tasks" / task_id)
    normalize_oracle_ground_truth(dest / "tasks" / task_id, task_id)
    assert_ground_truth_reachable(dest / "tasks" / task_id, task_id)
    config = dest / "config"
    config.mkdir(parents=True)
    write_json(config / "app.json", {
        "data_dir": f"{CONTAINER_BENCH_ROOT}/data",
        "tasks_dir": f"{CONTAINER_BENCH_ROOT}/tasks",
        "results_dir": f"{CONTAINER_BENCH_ROOT}/results",
        "work_root": CONTAINER_WORK_ROOT,
        "default_timeout_sec": 3600,
        "default_rounds": 1,
    })
    write_json(config / "harness.json", {"models": {"second-brain": {
        "adapter": "generic_cli",
        "command": "python",
        "model": model,
        "session_prefix": "second-brain-harnessbench",
        "args": [
            "/opt/sb-evals/isolated_driver.py",
            "--workspace", "{workspace}",
            "--prompt-file", "{prompt_file}",
            "--sandbox", "{sandbox}",
            "--task-id", "{task_id}",
            "--security-mode", mode,
            # The driver must finish *and write its bundle* before the
            # adapter's own ``subprocess.run(timeout=...)`` fires, because
            # upstream's ``run-task`` path does not catch ``TimeoutExpired``:
            # it propagates, no result file is written, and the oracle never
            # runs -- so a task the agent had mostly finished scores a hard
            # zero instead of the partial credit its workspace had earned.
            #
            # The margin has to cover everything after the turn ends, and
            # that is not free: paging the whole transcript back out of
            # ``conv.read``, reading the ledger, and SHA-256'ing every file
            # in the workspace. 30 seconds was cutting it fine on a task with
            # a large output tree.
            "--wall-seconds", str(max(30, task_timeout(source, task_id) - DRIVER_COLLECT_MARGIN_S)),
        ],
    }}})


def protect_benchmark_assets(container: str) -> None:
    targets = [f"{CONTAINER_BENCH_ROOT}/tasks", f"{CONTAINER_BENCH_ROOT}/grading"]
    owned = docker("exec", "-u", "0", container, "chown", "-R", "0:0", *targets)
    if owned.returncode:
        raise RuntimeError("could not take ownership of benchmark oracle assets: " + owned.stderr)
    protected = docker("exec", "-u", "0", container, "chmod", "-R", "go-rwx", *targets)
    if protected.returncode:
        raise RuntimeError("could not protect benchmark oracle assets: " + protected.stderr)
    # Node is an oracle dependency, not a bundled agent capability. The root
    # scorer can execute it; Second Brain (UID 1000) cannot and remains free to
    # install a workspace-local runtime using its own tools.
    hidden_runtime = docker("exec", "-u", "0", container, "chmod", "700", "/usr/bin/node")
    if hidden_runtime.returncode:
        raise RuntimeError("could not reserve Node for the scorer: " + hidden_runtime.stderr)


def task_timeout(source: Path, task_id: str) -> int:
    manifest = yaml.safe_load((source / "tasks" / task_id / "task.yaml").read_text(encoding="utf-8")) or {}
    return int(manifest.get("timeout_sec") or 600)


def run_container(name: str, image: str, env_file: Path,
                  overrides: dict[str, str] | None = None) -> None:
    """Start one task's server, with the job's configuration layered on top.

    ``--env-file`` supplies the standing settings and the API key; ``-e`` wins
    over it, which is what lets a job choose a model and a plugin profile
    without editing the file on disk or building an image per combination.
    """
    arguments = [
        "run", "-d", "--name", name, "--init", "--env-file", str(env_file),
        "-e", "SB_WRITABLE_DIRS=/work/harnessbench",
        "-e", "PYTHONUTF8=1", "-e", "PYTHONIOENCODING=utf-8",
    ]
    for key, value in (overrides or {}).items():
        # An empty override would otherwise *unset* the env-file value, which
        # is never what "the job did not specify this" should mean.
        if value not in (None, ""):
            arguments.extend(("-e", f"{key}={value}"))
    arguments.append(image)
    result = docker(*arguments)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or "docker run failed")


def copy_into(container: str, source: Path, target: str) -> None:
    docker("exec", "-u", "0", container, "mkdir", "-p", target)
    copied = docker("cp", str(source) + os.sep + ".", f"{container}:{target}")
    if copied.returncode:
        raise RuntimeError(copied.stderr or copied.stdout or "docker cp failed")
    docker("exec", "-u", "0", container, "chown", "-R", "1000:1000", target)


def stream_events(container: str, host_path: Path):
    host_path.parent.mkdir(parents=True, exist_ok=True)
    command = [docker_exe(), "exec", container, "sh", "-c", "touch /work/live/events.jsonl; tail -n +1 -F /work/live/events.jsonl"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")

    def copy_lines():
        with host_path.open("a", encoding="utf-8") as sink:
            assert process.stdout is not None
            for line in process.stdout:
                sink.write(line)
                sink.flush()
                print_live_event(line)

    thread = threading.Thread(target=copy_lines, daemon=True)
    thread.start()
    return process, thread


def print_live_event(line: str) -> None:
    try:
        event = json.loads(line)
        frame = event.get("frame") or {}
        kind = frame.get("kind")
        payload = frame.get("payload") or {}
        if kind == "stream_delta":
            delta = str(payload.get("delta") or "")
            if delta:
                sys.stdout.write(delta)
                sys.stdout.flush()
            if payload.get("done"):
                print(flush=True)
        elif kind == "tool_status":
            label = payload.get("tool_name") or payload.get("command_name") or payload.get("kind") or "tool"
            print(f"\n  [tool:{payload.get('status')}] {label}", flush=True)
        elif kind == "approval":
            print(f"\n  [approval] {payload.get('title')}", flush=True)
        elif kind == "error":
            print(f"\n  [agent error] {payload}", flush=True)
    except (TypeError, ValueError):
        return


def collect_container(container: str, task_dir: Path) -> None:
    """Copy the container's evidence out, replacing any earlier attempt's copy.

    **The destination is removed first, and that is load-bearing.** ``docker
    cp`` chooses between two behaviours based on whether the destination
    already exists: a missing destination is *created* holding the source's
    contents, while an existing one receives the source *nested inside it*.
    On a ``--retry-failed`` pass the first attempt has already created
    ``official-results/``, so a second copy lands at
    ``official-results/results/...`` and the tree now holds two results for
    one task. :func:`find_official_result` then refuses to guess between them
    and the retry is recorded as a harness error -- with a perfectly good
    oracle score sitting on disk.

    Removing first also keeps the bundle honest in the ordinary direction: a
    retry's evidence is the retry's, never a merge of two runs whose files
    happen not to collide.
    """
    if not container_exists(container):
        return
    for remote, local in (
        ("/work/harnessbench/results", task_dir / "official-results"),
        (CONTAINER_WORK_ROOT, task_dir / "sandboxes"),
        ("/work/live", task_dir / "live"),
    ):
        shutil.rmtree(local, ignore_errors=True)
        local.parent.mkdir(parents=True, exist_ok=True)
        docker("cp", f"{container}:{remote}", str(local), check=False)


def find_official_result(task_dir: Path, task_id: str) -> Path | None:
    candidates = list((task_dir / "official-results").rglob(task_id + ".json"))
    return candidates[0] if len(candidates) == 1 else None


def detect_provider_failure(task_dir: Path) -> str | None:
    text = ""
    for path in (task_dir / "container.log", task_dir / "harness.log"):
        try:
            text += "\n" + path.read_text(encoding="utf-8", errors="replace")[-20000:]
        except OSError:
            pass
    lowered = text.casefold()
    for pattern in PROVIDER_PATTERNS:
        if pattern in lowered:
            line = next((line for line in reversed(text.splitlines()) if pattern in line.casefold()), pattern)
            return line[-1000:]
    return None


def task_score(status: dict[str, Any] | None) -> float:
    """The one number a scheduled task contributes to the mean.

    **The single definition, deliberately.** ``compare_harness_runs`` imports
    this rather than reimplementing it: two scorers that disagree about what
    an unfinished task is worth produce a comparison whose per-task deltas do
    not add up to the aggregate printed beside them, and the disagreement is
    invisible because both numbers look reasonable on their own.

    Everything that is not a complete run with a numeric oracle score is
    zero. That is harsher than it needs to be for a harness error and it is
    meant to be: a benchmark that quietly excuses its own failures reports a
    mean over the runs that happened to work.
    """
    status = status or {}
    if status.get("state") != "complete":
        return 0.0
    value = status.get("outcome_score")
    # ``bool`` is an ``int``. An oracle answering True/False is answering a
    # score of 1.0/0.0, which is what float() already makes of it.
    return float(value) if isinstance(value, (int, float)) else 0.0


def write_summary(run_dir: Path, task_ids: list[str]) -> dict[str, Any]:
    """Refresh ``summary.json``, and never let that failure end the run.

    The summary is **derived** -- every number in it is recomputed from the
    per-task ``status.json`` files, which are the real record. Losing a write
    costs a stale file until the next task finishes, or one ``--resume``.
    Losing the run costs whatever the provider has already been paid.

    So this reports and continues. The asymmetry is the whole point: an
    unwritable derived file is an inconvenience, and it used to be a crash
    that discarded a suite mid-flight.
    """
    summary = summarize(run_dir, task_ids)
    try:
        write_json(run_dir / "summary.json", summary)
    except OSError as exc:                                   # noqa: BLE001
        print(f"warning: could not update summary.json ({exc}); "
              "the run continues and status.json remains authoritative",
              file=sys.stderr)
    return summary


def summarize(run_dir: Path, task_ids: list[str]) -> dict[str, Any]:
    rows = []
    scores: list[float] = []
    unscored = 0
    llm_calls = 0
    # Provider-reported counts, kept per bucket with their own call tallies so
    # a total is only ever published beside how many calls actually answered.
    token_totals = {"input": 0, "cached": 0, "output": 0}
    token_calls = {"input": 0, "cached": 0, "output": 0}
    for task_id in task_ids:
        status = read_json(run_dir / "tasks" / task_id / "status.json") or {"task_id": task_id, "state": "pending"}
        rows.append(status)
        # Every scheduled task contributes exactly one score, and the only
        # question is whether it is the oracle's or a zero. Anything short of
        # a complete run scores zero, and so does a *complete* run whose
        # oracle declined to produce a number -- an unscored task silently
        # dropped from the list would shrink the divisor rather than the
        # score, which is precisely the exception filtering the denominator
        # is meant to prevent. ``unscored`` is reported beside the mean so a
        # reader can tell "the agent failed 3" from "the oracle answered
        # nothing 3 times", which are very different problems.
        scores.append(task_score(status))
        if status.get("state") == "complete" and not isinstance(
                status.get("outcome_score"), (int, float)):
            unscored += 1
        for event in read_jsonl(run_dir / "tasks" / task_id / "events.jsonl"):
            if event.get("source") != "llm" or event.get("kind") != "llm_call":
                continue
            llm_calls += 1
            payload = event.get("payload") or {}
            for field, bucket in (("prompt_tokens", "input"),
                                  ("cached_prompt_tokens", "cached"),
                                  ("completion_tokens", "output")):
                value = payload.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    token_totals[bucket] += int(value)
                    token_calls[bucket] += 1
    return {
        "run_id": run_dir.name,
        "task_count": len(task_ids),
        "completed": sum(1 for row in rows if row.get("state") == "complete"),
        "harness_errors": sum(1 for row in rows if row.get("state") == "harness_error"),
        "provider_unavailable": sum(1 for row in rows if row.get("state") == "provider_unavailable"),
        # Progress over the *scheduled* set: a task not yet reached counts as
        # zero here on purpose, because this number answers "how far through
        # the planned work are we". The database's ``config_scores`` answers
        # the different question of how well the attempted tasks went and
        # excludes unrun ones. Reporting ``attempted`` beside the score is
        # what keeps the two from being mistaken for each other.
        "completion_score": round(statistics.mean(scores), 6) if scores else None,
        "score_denominator": len(task_ids),
        "attempted": sum(1 for row in rows if row.get("state")),
        "completed_without_oracle_score": unscored,
        # ``input_tokens_billed`` is the sum of each call's whole prompt --
        # what the provider charges for -- and not the context size. A total
        # stays ``None`` when no call reported it, because a missing count
        # read as zero understates cost without ever looking wrong.
        "llm_usage": {
            "calls": llm_calls,
            "calls_with_prompt_tokens": token_calls["input"],
            "calls_with_completion_tokens": token_calls["output"],
            "input_tokens_billed": token_totals["input"] if token_calls["input"] else None,
            "cached_input_tokens": token_totals["cached"] if token_calls["cached"] else None,
            "output_tokens": token_totals["output"] if token_calls["output"] else None,
            "input_complete": bool(llm_calls) and token_calls["input"] == llm_calls,
            "output_complete": bool(llm_calls) and token_calls["output"] == llm_calls,
        },
        "tasks": rows,
        "updated_at": time.time(),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\nHarness-Bench deterministic completion")
    print(f"  score: {summary.get('completion_score')} over {summary.get('score_denominator')} scheduled tasks")
    print(f"  completed: {summary.get('completed')}  harness errors: {summary.get('harness_errors')}  provider unavailable: {summary.get('provider_unavailable')}")


def provider_preflight(model: str, values: dict[str, str]) -> None:
    key = values.get("SB_LLM_API_KEY")
    endpoint = values.get("SB_LLM_ENDPOINT")
    missing = [name for name, value in (
        ("SB_LLM_API_KEY", key), ("SB_LLM_ENDPOINT", endpoint)
    ) if not value]
    if missing:
        raise RuntimeError("provider preflight is missing " + ", ".join(missing))
    try:
        import litellm

        litellm.drop_params = True
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Reply OK."}],
            api_key=key,
            api_base=endpoint,
            max_tokens=1,
            timeout=30,
        )
    except Exception as exc:
        message = " ".join(str(exc).split())[-1200:]
        raise ProviderUnavailableError(message) from None
    print(f"Provider preflight passed for {model}.", flush=True)


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def inspect_image(image: str) -> str:
    result = docker("image", "inspect", image, "--format", "{{.Id}}")
    if result.returncode:
        raise RuntimeError(f"image {image!r} is missing; run with --build-image first")
    return result.stdout.strip()


def docker(*args: str, check: bool = True):
    result = subprocess.run([docker_exe(), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout or "docker command failed")
    return result


def docker_exe() -> str:
    found = shutil.which("docker")
    if found:
        return found
    desktop = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    if desktop.exists():
        return str(desktop)
    raise FileNotFoundError("docker executable not found")


def container_exists(name: str) -> bool:
    return docker("inspect", name, check=False).returncode == 0


def container_logs(name: str) -> str:
    if not container_exists(name):
        return ""
    result = docker("logs", name, check=False)
    return (result.stdout or "") + (result.stderr or "")


def run_logged(command: list[str], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as sink:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for line in process.stdout:
            sink.write(line)
            sink.flush()
        return process.wait()


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def safe_container_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)[:63]


def append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_json_text(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def write_json(path: Path, payload: Any, *, attempts: int = 12) -> None:
    """Write atomically, retrying the swap while a reader holds the target.

    **Windows cannot replace a file another process has open.** Python's
    ``open()`` does not request ``FILE_SHARE_DELETE``, so any concurrent
    reader makes ``os.replace`` fail with ``PermissionError`` (WinError 5) --
    and this file is read once per second by the viewer's ``load_state`` while
    the Live tab is open. Watching a run was therefore enough to kill it: the
    launcher crashed mid-suite writing ``summary.json``, discarding a paid
    run because somebody was looking at it.

    A reader holds the file for microseconds, so a short backoff clears it.
    The write itself is still atomic -- the retry is on the swap, never on
    the content, so no reader ever sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8")
    for attempt in range(attempts):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))       # ~3.9s over 12 attempts


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
