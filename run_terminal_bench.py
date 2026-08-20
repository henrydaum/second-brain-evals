"""Stable launcher for Terminal-Bench through Harbor.

Examples:
    python run_terminal_bench.py --smoke terminal-bench/break-filter-js-from-html
    python run_terminal_bench.py --full --attempts 1 --concurrency 2
"""

from __future__ import annotations

import argparse
import os
import json
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness.payload import prepare_payload

ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = "terminal-bench/terminal-bench-2"
AGENT = "evals.terminal_bench.agent:SecondBrainAgent"
PROVIDER_SENTINEL = ".provider-unavailable.json"
PROVIDER_ERROR_TYPE = "ProviderUnavailableError"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", metavar="TASK", help="run one named Terminal-Bench task")
    mode.add_argument("--full", action="store_true", help="run the complete dataset")
    mode.add_argument(
        "--resume",
        metavar="JOB_DIR",
        help="resume an interrupted Harbor job after the provider preflight passes",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--model", default=None, help="defaults to SB_LLM_MODEL in the env file")
    parser.add_argument("--env-file", default="bench.env")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--retries", type=int, default=2,
                        help="retry transient Harbor/agent failures (default: 2)")
    parser.add_argument("--job-name", default=None)
    parser.add_argument("--force-payload", action="store_true")
    parser.add_argument(
        "--skip-provider-check",
        action="store_true",
        help="skip the one-token model/API preflight (not recommended)",
    )
    parser.add_argument("--harbor-arg", action="append", default=[], help="extra raw Harbor argument")
    options = parser.parse_args(argv)

    env_file = Path(options.env_file).resolve()
    if not env_file.exists():
        parser.error(f"env file does not exist: {env_file}")
    values = _read_env(env_file)
    model = options.model or values.get("SB_LLM_MODEL")
    if not model:
        parser.error("no model supplied and SB_LLM_MODEL is absent from the env file")

    if not options.skip_provider_check:
        try:
            _check_provider(model, values)
        except RuntimeError as exc:
            parser.error(str(exc))

    payload = prepare_payload(force=options.force_payload)
    env = dict(os.environ)
    env.update(values)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    _add_docker_to_path(env)

    harbor = _harbor_executable()
    if options.resume:
        job_dir = Path(options.resume).resolve()
        try:
            _validate_resume(job_dir, payload)
        except RuntimeError as exc:
            parser.error(str(exc))
        command = [
            harbor,
            "job",
            "resume",
            "--job-path", str(job_dir),
            "--filter-error-type", "CancelledError",
            "--filter-error-type", PROVIDER_ERROR_TYPE,
        ]
    else:
        job_name = options.job_name or (
            "second-brain-smoke" if options.smoke else "second-brain-terminal-bench"
        )
        job_dir = ROOT / "results" / "harbor" / job_name
        command = [
            harbor,
            "run",
            "--dataset", options.dataset,
            "--agent", AGENT,
            "--model", model,
            "--env-file", str(env_file),
            "--n-attempts", str(options.attempts),
            "--n-concurrent", str(options.concurrency),
            "--max-retries", str(options.retries),
            "--retry-exclude", PROVIDER_ERROR_TYPE,
            "--job-name", job_name,
            "--jobs-dir", str(ROOT / "results" / "harbor"),
            "--yes",
        ]
        if options.smoke:
            command += ["--include-task-name", options.smoke]
    command += options.harbor_arg

    marker = job_dir / PROVIDER_SENTINEL
    marker.unlink(missing_ok=True)
    print("Running:", subprocess.list2cmdline(command), flush=True)
    return _run_guarded(command, job_dir=job_dir, env=env)


def _harbor_executable() -> str:
    candidates = [ROOT / ".venv" / "Scripts" / "harbor.exe", ROOT / ".venv" / "bin" / "harbor"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("harbor")
    if found:
        return found
    raise FileNotFoundError("Harbor is not installed; run `uv sync` in this repository")


def _add_docker_to_path(env: dict[str, str]) -> None:
    if shutil.which("docker", path=env.get("PATH")):
        return
    desktop = Path(r"C:\Program Files\Docker\Docker\resources\bin")
    if (desktop / "docker.exe").exists():
        env["PATH"] = str(desktop) + os.pathsep + env.get("PATH", "")


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _validate_resume(job_dir: Path, payload: Path) -> None:
    config = job_dir / "config.json"
    if not config.exists():
        raise RuntimeError(f"Harbor job config does not exist: {config}")

    current = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))["identity"]
    previous = set()
    for result_path in job_dir.glob("*/result.json"):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = (result.get("agent_info") or {}).get("version")
        if version:
            previous.add(str(version))
    if previous and previous != {current}:
        shown = ", ".join(sorted(previous))
        raise RuntimeError(
            "refusing to mix Second Brain payload versions in one score: "
            f"job has {shown}; current payload is {current}"
        )


def _run_guarded(command: list[str], *, job_dir: Path, env: dict[str, str]) -> int:
    """Run Harbor and pause the job as soon as a provider sentinel appears."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(command, cwd=ROOT, env=env, creationflags=creationflags)
    marker = job_dir / PROVIDER_SENTINEL
    try:
        while process.poll() is None:
            if marker.exists():
                try:
                    detail = json.loads(marker.read_text(encoding="utf-8"))
                    reason = detail.get("diagnostic") or "provider unavailable"
                except (OSError, ValueError):
                    reason = "provider unavailable"
                print(
                    "\nPausing Harbor before more trials are scheduled: " + str(reason),
                    flush=True,
                )
                _interrupt(process)
                return 75
            time.sleep(1)
        return int(process.returncode or 0)
    except KeyboardInterrupt:
        print("\nPausing Harbor; completed trials will be resumable.", flush=True)
        _interrupt(process)
        return 130


def _interrupt(process: subprocess.Popen, grace_seconds: int = 30) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _check_provider(model: str, values: dict[str, str]) -> None:
    """Make one tiny call before Harbor schedules expensive task containers.

    A valid HTTP endpoint is not enough: provider accounts can authenticate
    successfully while their token plan or credit balance is exhausted.  In
    that state every Terminal-Bench trial would otherwise become a misleading
    zero.  LiteLLM is the benchmark backend, so using the same client here also
    checks its provider routing and model spelling.
    """
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
        # Exception text is useful (status, provider code, quota reason) and
        # LiteLLM does not include the API key in it.  Keep the prefix explicit
        # so CI and humans know this is not a benchmark score.
        raise RuntimeError(f"model provider preflight failed: {exc}") from exc

    print(f"Provider preflight passed for {model}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
