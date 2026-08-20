"""Configure, start, and drive Second Brain inside a benchmark container."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALL_ROOT = Path("/opt/second-brain-agent")
APP_ROOT = INSTALL_ROOT / "app"
STATE_ROOT = INSTALL_ROOT / "state"
DATA_DIR = STATE_ROOT / "Second Brain"
SEED_DIR = INSTALL_ROOT / "seed"

# paths.py resolves this at import time, so it must be present before configure
# imports any Second Brain module.
os.environ["XDG_DATA_HOME"] = str(STATE_ROOT)


def configure(project: Path) -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SEED_DIR, DATA_DIR)

    sys.path.insert(0, str(APP_ROOT))
    from config import config_manager

    config = config_manager.load()
    model = _required("SB_LLM_MODEL")
    config.update(
        {
            "enabled_frontends": ["http"],
            "secret_http_token": _required("SB_HTTP_TOKEN"),
            "http_port": int(os.environ.get("SB_HTTP_PORT", "8787")),
            "db_path": str(DATA_DIR / "database.db"),
            "default_llm_profile": model,
            "default_tool_max_calls": int(os.environ.get("SB_TOOL_CALL_LIMIT", "100")),
            "fs_writable_dirs": [str(project)],
            "shell_allowed_prefixes": [],
            "sync_directories": [str(project)],
        }
    )
    profiles = dict(config.get("llm_profiles") or {})
    profiles[model] = {
        "llm_endpoint": os.environ.get("SB_LLM_ENDPOINT", ""),
        "secret_llm_api_key": _required("SB_LLM_API_KEY"),
        "llm_context_size": int(os.environ.get("SB_LLM_CONTEXT", "0")),
        "llm_service_class": os.environ.get("SB_LLM_BACKEND", "LiteLLMService"),
        "llm_capabilities": {"image": False, "audio": False, "video": False},
    }
    config["llm_profiles"] = profiles
    config_manager.save(config)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--task-id", default="terminal-bench")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    configure(project)
    env = dict(os.environ)
    env.update(
        {
            "XDG_DATA_HOME": str(STATE_ROOT),
            "SB_PROJECT_DIR": str(project),
            "SB_TEMPLATE_MANIFEST": str(INSTALL_ROOT / "manifest.json"),
            "SB_LLM_USAGE_LOG": str(Path(args.result_dir) / "llm_usage.jsonl"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    app_log = Path(args.result_dir) / "second-brain.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    with app_log.open("w", encoding="utf-8") as log:
        app = subprocess.Popen(
            [sys.executable, "main.py"], cwd=APP_ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            spec_path = Path(args.result_dir) / "task.json"
            prompt = Path(args.instruction_file).read_text(encoding="utf-8")
            spec = {
                "id": args.task_id,
                "prompt": (
                    "You are being evaluated in a terminal task. Work directly in "
                    f"{project}. Complete the task below using your tools. Inspect the "
                    "environment, make the required changes, and run relevant checks. "
                    "Do not merely describe a solution; leave the task completed on disk.\n\n"
                    + prompt
                ),
                "workdir": str(project),
                "base": "http://127.0.0.1:8787",
                "manifest": {"default": "allow"},
                "budget": {
                    "wall_s": float(os.environ.get("SB_TASK_TIMEOUT", "900")),
                    "stall_s": float(os.environ.get("SB_STALL_TIMEOUT", "300")),
                },
            }
            spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
            driver = INSTALL_ROOT / "driver" / "run_task.py"
            return subprocess.call(
                [sys.executable, str(driver), str(spec_path), "--out", args.result_dir],
                env=env,
            )
        finally:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=5)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is missing")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
