"""Generic-CLI bridge used by Harness-Bench inside the task container.

Harness-Bench invokes this command once per prompt round. The Second Brain
server remains alive in the same container, so later rounds reuse the same
thread and conversation while separate benchmark tasks still receive fresh
containers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/opt/sb-driver")

from driver.run_task import main as drive_task  # noqa: E402


ROUND_RE = re.compile(r"round[-_]?([0-9]+)", re.IGNORECASE)


def _round_number(prompt_file: Path) -> int:
    match = ROUND_RE.search(prompt_file.stem)
    return int(match.group(1)) if match else 1


def _manifest(mode: str, workspace: Path) -> dict:
    if mode == "mediated":
        return {
            "default": "deny",
            "fs.write": {"under": [str(workspace)]},
            "fs.delete": {"under": [str(workspace)]},
            "fs.move": {"under": [str(workspace)]},
            "script.run": "allow",
        }
    # YOLO and lockdown are enforced by the kernel session mode. Default-deny
    # remains a safe fallback if a mode transition fails.
    return {"default": "deny"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--security-mode",
        choices=("yolo", "lockdown", "mediated"),
        required=True,
    )
    parser.add_argument("--wall-seconds", type=float, default=870.0)
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    prompt_file = Path(args.prompt_file).resolve()
    sandbox = Path(args.sandbox).resolve()
    round_number = _round_number(prompt_file)
    out = sandbox / "second-brain" / f"round-{round_number:02d}"
    out.mkdir(parents=True, exist_ok=True)

    requested_mode = "ask" if args.security_mode == "mediated" else args.security_mode
    task_prompt = prompt_file.read_text(encoding="utf-8")
    workspace_instruction = (
        f"Benchmark workspace: `{workspace}`. Treat this directory as the task's "
        "working directory, not `/app`. For tools that accept a path, resolve "
        "relative task paths under this workspace. For `run_command`, set its "
        f"`cwd` argument to `{workspace}` on the first call; that directory then "
        "persists for later shell calls. Do not search `/app` for task inputs.\n\n"
    )
    spec = {
        "id": args.task_id,
        "prompt": workspace_instruction + task_prompt,
        "workdir": str(workspace),
        "thread": "main",
        "fresh_session": round_number == 1,
        "security_mode": requested_mode,
        "manifest": _manifest(args.security_mode, workspace),
        "ui": {"policy": "canned", "text": "Proceed using the supplied task materials."},
        "budget": {
            "wall_s": float(os.environ.get("SB_TASK_TIMEOUT") or args.wall_seconds),
            "stall_s": float(os.environ.get("SB_STALL_TIMEOUT", "300")),
        },
    }
    spec_path = out / "task.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return drive_task([str(spec_path), "--out", str(out)])


if __name__ == "__main__":
    raise SystemExit(main())
