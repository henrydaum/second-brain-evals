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
DEFAULT_BENCHMARK = ROOT / "build" / "harness-bench-src"
DEFAULT_IMAGE = "second-brain:harness-bench"
RESULTS = ROOT / "results" / "harness-bench"
BENCHMARK_TOOLS = {
    "edit_file", "glob", "grep", "read_file", "run_command", "run_script",
    "schedule_subagent", "spawn_subagent", "sql_query", "validate", "web_search",
}
CONTAINER_BENCH_ROOT = "/work/harnessbench"
# run_command deliberately accepts cwd only under Second Brain's application
# or data roots. Keeping official task sandboxes in the agent data workspace
# makes the real workspace reachable without weakening that production rule.
CONTAINER_WORK_ROOT = "/data/Second Brain/workspace/harnessbench-sandboxes"
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
    parser.add_argument("--mode", choices=("yolo", "lockdown", "mediated"), default="yolo")
    parser.add_argument("--benchmark-root", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--fetch-benchmark", action="store_true", help="clone/fetch the exact pinned upstream revision")
    parser.add_argument("--env-file", default="bench.env")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--build-image", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="run official demo oracle and mode reuse without a model call")
    parser.add_argument("--execute", action="store_true", help="authorize provider-backed task execution")
    parser.add_argument("--skip-provider-check", action="store_true")
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
    if not options.execute:
        print("No model calls made. Add --execute to run: " + ", ".join(tasks))
        return 0

    env_file = Path(options.env_file).resolve()
    if not env_file.is_file():
        parser.error(f"env file does not exist: {env_file}")
    env_values = read_env(env_file)
    model = env_values.get("SB_LLM_MODEL")
    if not model:
        parser.error("SB_LLM_MODEL is missing from the env file")
    if not options.skip_provider_check:
        try:
            provider_preflight(model, env_values)
        except ProviderUnavailableError as exc:
            print(f"Provider unavailable; no benchmark task started: {exc}", file=sys.stderr)
            return 75

    image_id = inspect_image(options.image)
    run_dir, run = open_run(options, tasks, model, metadata, image_id)
    print(f"Harness-Bench run {run['run_id']}: {len(tasks)} tasks, {options.mode}, Essentials tools, {model}")
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
        )
        write_json(run_dir / "summary.json", summarize(run_dir, tasks))
        if result.get("state") == "provider_unavailable":
            print("Provider quota/credit failure detected; stopping before another task is scheduled.")
            exit_code = 75
            break
        if result.get("state") not in ("complete",):
            exit_code = 1

    summary = summarize(run_dir, tasks)
    write_json(run_dir / "summary.json", summary)
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
        subprocess.run(["git", "clone", LOCK["repository"], str(root)], check=True)
    elif not (root / ".git").is_dir():
        raise RuntimeError(f"benchmark root exists but is not a Git checkout: {root}")
    current = git(root, "rev-parse", "HEAD")
    if current != LOCK["commit"]:
        subprocess.run(["git", "-C", str(root), "fetch", "origin", LOCK["commit"]], check=True)
        subprocess.run(["git", "-C", str(root), "checkout", "--detach", LOCK["commit"]], check=True)


def choose_tasks(options: argparse.Namespace, available: dict[str, Any]) -> list[str]:
    chosen = options.task or (SELECTIONS["pilot"] if options.pilot else SELECTIONS["smoke"])
    unknown = [task for task in chosen if task not in available]
    if unknown:
        raise RuntimeError("unknown Harness-Bench task(s): " + ", ".join(unknown))
    return list(dict.fromkeys(chosen))


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
            command = (
                "PYTHONPATH=/work/harnessbench/src "
                "HARNESSBENCH_APP_CONFIG=/work/harnessbench/config/app.json "
                "HARNESSBENCH_HARNESS_CONFIG=/work/harnessbench/config/harness.json "
                "HARNESSBENCH_SKIP_PROCESS_GRADE=1 "
                "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1 "
                "python -m harnessbench.cli run-task --task 001-file --harness demo --mode demo"
            )
            demo = docker("exec", name, "sh", "-c", command)
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
            agent = docker("exec", name, "sh", "-c", agent_command)
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
        expected = {"mode": options.mode, "model": model,
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
    template = read_json(ROOT / "template" / "template_manifest.json")
    run = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": time.time(),
        "mode": options.mode,
        "model": model,
        "tool_profile": "bundle_essentials minus telegram, ask_question, show_files",
        "visible_tools": sorted(BENCHMARK_TOOLS),
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
        "reported_metric": "Harness-Bench deterministic completion",
    }
    return run_dir, run


def run_one_task(*, task_id, task_dir, benchmark, env_file, image, mode, model,
                 keep_container, position):
    task_dir.mkdir(parents=True, exist_ok=True)
    status = {"task_id": task_id, "state": "running", "started_at": time.time(), "mode": mode, "model": model}
    write_json(task_dir / "status.json", status)
    container = safe_container_name(f"sb-hb-{task_id}-{uuid.uuid4().hex[:6]}")
    tail = None
    event_thread = None
    returncode = None
    try:
        with tempfile.TemporaryDirectory(prefix="sb-harnessbench-") as temp:
            stage = Path(temp) / "harnessbench"
            stage_benchmark(benchmark, stage, task_id, mode, model)
            run_container(container, image, env_file)
            copy_into(container, stage, "/work/harnessbench")
            tail, event_thread = stream_events(container, task_dir / "events.jsonl")
            print(f"[{position}] {task_id}: running", flush=True)
            command = [
                docker_exe(), "exec",
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
    if provider_error:
        status["state"] = "provider_unavailable"
        status["provider_error"] = provider_error
    elif official:
        status["state"] = "complete"
        status["official_result"] = str(official.relative_to(task_dir)).replace("\\", "/")
        payload = read_json(official) or {}
        status["outcome_score"] = ((payload.get("oracle_result") or {}).get("outcome_score"))
        status["combined_score"] = ((payload.get("scoring") or {}).get("combined_score"))
        status["adapter_ok"] = ((payload.get("adapter_result") or {}).get("ok"))
        status["elapsed_sec"] = payload.get("elapsed_sec")
    elif status.get("state") == "running":
        status["state"] = "harness_error"
        status["error"] = f"Harness-Bench exited {returncode} without a result"
    status["finished_at"] = time.time()
    write_json(task_dir / "status.json", status)
    append_event(task_dir / "events.jsonl", {"at": time.time(), "source": "harness", "kind": "task_result", "status": status})
    score = status.get("outcome_score")
    print(f"[{position}] {task_id}: {status['state']} score={score}", flush=True)
    return status


def stage_benchmark(source: Path, dest: Path, task_id: str, mode: str, model: str) -> None:
    shutil.copytree(source / "src", dest / "src")
    shutil.copytree(source / "grading", dest / "grading")
    shutil.copytree(source / "tasks" / task_id, dest / "tasks" / task_id)
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
            "/opt/sb-evals/drive_round.py",
            "--workspace", "{workspace}",
            "--prompt-file", "{prompt_file}",
            "--sandbox", "{sandbox}",
            "--task-id", "{task_id}",
            "--security-mode", mode,
            "--wall-seconds", str(max(30, task_timeout(source, task_id) - 30)),
        ],
    }}})


def task_timeout(source: Path, task_id: str) -> int:
    manifest = yaml.safe_load((source / "tasks" / task_id / "task.yaml").read_text(encoding="utf-8")) or {}
    return int(manifest.get("timeout_sec") or 600)


def run_container(name: str, image: str, env_file: Path) -> None:
    result = docker(
        "run", "-d", "--name", name, "--init", "--env-file", str(env_file),
        "-e", "SB_WRITABLE_DIRS=/work/harnessbench",
        "-e", "PYTHONUTF8=1", "-e", "PYTHONIOENCODING=utf-8", image,
    )
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
    if not container_exists(container):
        return
    for remote, local in (
        ("/work/harnessbench/results", task_dir / "official-results"),
        (CONTAINER_WORK_ROOT, task_dir / "sandboxes"),
        ("/work/live", task_dir / "live"),
    ):
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


def summarize(run_dir: Path, task_ids: list[str]) -> dict[str, Any]:
    rows = []
    scores = []
    llm_calls = 0
    prompt_tokens = 0
    calls_with_tokens = 0
    for task_id in task_ids:
        status = read_json(run_dir / "tasks" / task_id / "status.json") or {"task_id": task_id, "state": "pending"}
        rows.append(status)
        value = status.get("outcome_score") if status.get("state") == "complete" else 0.0
        if isinstance(value, (int, float)):
            scores.append(float(value))
        for event in read_jsonl(run_dir / "tasks" / task_id / "events.jsonl"):
            if event.get("source") != "llm" or event.get("kind") != "llm_call":
                continue
            llm_calls += 1
            tokens = (event.get("payload") or {}).get("prompt_tokens")
            if isinstance(tokens, (int, float)):
                prompt_tokens += int(tokens)
                calls_with_tokens += 1
    return {
        "run_id": run_dir.name,
        "task_count": len(task_ids),
        "completed": sum(1 for row in rows if row.get("state") == "complete"),
        "harness_errors": sum(1 for row in rows if row.get("state") == "harness_error"),
        "provider_unavailable": sum(1 for row in rows if row.get("state") == "provider_unavailable"),
        "completion_score": round(statistics.mean(scores), 6) if scores else None,
        "score_denominator": len(task_ids),
        "llm_usage": {
            "calls": llm_calls,
            "calls_with_prompt_tokens": calls_with_tokens,
            "prompt_tokens_total_known": prompt_tokens if calls_with_tokens else None,
            "completion_tokens": None,
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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
