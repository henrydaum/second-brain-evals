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

    # Tell run_command where this task's work happens, before the first
    # command rather than after it fails. ``entrypoint`` pointed the
    # ``initial_working_directory`` setting at this file; the sandbox path
    # only exists now, which is why the setting names a file and not a
    # directory. Best-effort: if it cannot be written the tool falls back to
    # the project root, which is exactly the old behaviour.
    pointer = Path(os.environ.get("SB_INITIAL_CWD_FILE", "/work/live/cwd"))
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(workspace) + "\n", encoding="utf-8")
        pointer.chmod(0o644)
    except OSError as exc:
        print(f"[cwd] could not publish {workspace} to {pointer}: {exc}", flush=True)

    requested_mode = "ask" if args.security_mode == "mediated" else args.security_mode
    task_prompt = prompt_file.read_text(encoding="utf-8")
    # ``run_command`` now starts in the workspace by itself, so this says where
    # the shell already is rather than asking for a `cwd` argument the model
    # forgot about a third of the time -- 103 of 286 shell failures across 27
    # runs were commands that ran in `/app`. The path is still named, because
    # the file tools take absolute paths and because a failed pointer write
    # must leave the agent able to find its own workspace.
    workspace_instruction = (
        f"Benchmark workspace: `{workspace}`. Your shell already starts there, "
        "and that directory persists across `run_command` calls, so relative "
        "paths work without passing `cwd`. For tools that take a path, resolve "
        "relative task paths under this workspace. Do not search `/app` for "
        "task inputs.\n\n"
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
        # ``watch`` is the measurement and is always on -- it is one hash of a
        # small tree and it is the only record of whether the agent rewrote
        # its inputs. ``correct`` is the intervention: an extra turn naming
        # what changed. Off by default so a run measures the guard rather
        # than assuming it, and enabled per run with SB_INTEGRITY_CORRECT=1.
        "integrity": {
            "watch": os.environ.get("SB_INTEGRITY_WATCH", "1") != "0",
            "correct": os.environ.get("SB_INTEGRITY_CORRECT", "") in ("1", "true", "yes"),
            "commit": os.environ.get("SB_COMMIT_NUDGE", "") in ("1", "true", "yes"),
        },
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
