"""Build the portable Second Brain payload installed into eval environments.

Harbor owns the task container.  The payload is deliberately application files,
not a Docker image: an agent running in a sidecar would execute commands against
the wrong filesystem and Terminal-Bench's verifier would score an untouched task.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECOND_BRAIN_REPO = ROOT.parent / "Second Brain"
PAYLOAD_DIR = ROOT / "build" / "second-brain-agent"

SOURCE_DIRS = (
    "agent",
    "attachments",
    "bundled",
    "config",
    "events",
    "llm",
    "parsing",
    "pipeline",
    "plugins",
    "runtime",
    "sandbox",
    "state_machine",
    "templates",
)
SOURCE_FILES = (
    "main.py",
    "main.pyw",
    "migrations.py",
    "paths.py",
    "trees.py",
)
RUNTIME_REQUIREMENTS = (
    "watchdog",
    "cron-descriptor",
    "croniter",
    "litellm==1.97.0",
    "Pillow",
)


def second_brain_repo() -> Path:
    return Path(os.environ.get("SECOND_BRAIN_REPO", DEFAULT_SECOND_BRAIN_REPO)).resolve()


def git_revision(repo: Path, ref: str = "HEAD") -> str:
    answer = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if answer.returncode:
        raise RuntimeError(answer.stderr.strip() or f"could not resolve {ref} in {repo}")
    return answer.stdout.strip()


def prepare_payload(force: bool = False) -> Path:
    """Create a deterministic, secret-free install tree and return its path."""
    repo = second_brain_repo()
    template = ROOT / "template" / "Second Brain"
    template_manifest = ROOT / "template" / "template_manifest.json"
    _validate_inputs(repo, template, template_manifest)

    kernel_commit = git_revision(repo)
    store_commit = git_revision(repo, "origin/store")
    identity = _identity(repo, template, kernel_commit, store_commit)
    manifest_path = PAYLOAD_DIR / "manifest.json"
    if not force and manifest_path.exists():
        try:
            if json.loads(manifest_path.read_text(encoding="utf-8")).get("identity") == identity:
                return PAYLOAD_DIR
        except (OSError, ValueError):
            pass

    if PAYLOAD_DIR.exists():
        shutil.rmtree(PAYLOAD_DIR)
    app = PAYLOAD_DIR / "app"
    app.mkdir(parents=True)
    for name in SOURCE_DIRS:
        shutil.copytree(repo / name, app / name, ignore=_ignore_generated)
    for name in SOURCE_FILES:
        shutil.copy2(repo / name, app / name)

    # Second Brain normally treats its own source checkout as the project.
    # In an eval, the benchmark workdir is the project and the framework lives
    # under /opt.  This payload-only patch keeps that concern out of the kernel.
    main = app / "main.pyw"
    source = main.read_text(encoding="utf-8")
    old = "_ROOT = Path(__file__).parent"
    new = "_ROOT = Path(os.environ.get('SB_PROJECT_DIR') or Path(__file__).parent)"
    if source.count(old) != 1:
        raise RuntimeError("Second Brain main.pyw no longer has the expected project-root declaration")
    main.write_text(source.replace(old, new), encoding="utf-8", newline="\n")

    # Observe the kernel's existing LLM-finished event without changing the
    # framework's prompts or call path.  The Unix wrapper runs before main.pyw,
    # which is early enough to see every main-agent and subagent call.
    telemetry = ROOT / "harness" / "eval_telemetry.py"
    shutil.copy2(telemetry, app / "eval_telemetry.py")
    wrapper = app / "main.py"
    wrapper_source = wrapper.read_text(encoding="utf-8")
    wrapper_needle = "import runpy\n"
    wrapper_insert = (
        "import runpy\n\n"
        "from eval_telemetry import install as _install_eval_telemetry\n"
        "_install_eval_telemetry()\n"
    )
    if wrapper_source.count(wrapper_needle) != 1:
        raise RuntimeError("Second Brain main.py no longer has the expected runpy import")
    wrapper.write_text(
        wrapper_source.replace(wrapper_needle, wrapper_insert),
        encoding="utf-8",
        newline="\n",
    )

    shutil.copytree(template, PAYLOAD_DIR / "seed")
    shutil.copytree(ROOT / "driver", PAYLOAD_DIR / "driver", ignore=_ignore_generated)
    shutil.copy2(ROOT / "harness" / "runtime_entry.py", PAYLOAD_DIR / "runtime_entry.py")
    (PAYLOAD_DIR / "requirements.txt").write_text(
        "\n".join(RUNTIME_REQUIREMENTS) + "\n", encoding="utf-8", newline="\n"
    )
    template_data = json.loads(template_manifest.read_text(encoding="utf-8"))
    manifest = {
        "identity": identity,
        "kernel_commit": kernel_commit,
        "store_commit": store_commit,
        "template": template_data,
        "runtime_requirements": list(RUNTIME_REQUIREMENTS),
        "project_root_patch": True,
        "llm_usage_telemetry": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return PAYLOAD_DIR


def _identity(repo: Path, template: Path, kernel_commit: str, store_commit: str) -> str:
    """Hash what will actually execute, including uncommitted local changes."""
    digest = hashlib.sha256()
    digest.update(f"payload-format=3\nkernel={kernel_commit}\nstore={store_commit}\n".encode())
    roots = [*(repo / name for name in SOURCE_DIRS), template, ROOT / "driver"]
    files = [*(repo / name for name in SOURCE_FILES),
             ROOT / "harness" / "runtime_entry.py",
             ROOT / "harness" / "eval_telemetry.py", Path(__file__)]
    for root in roots:
        files.extend(path for path in root.rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"))
    for path in sorted(files, key=lambda item: str(item).casefold()):
        if path.is_relative_to(repo):
            label = "app/" + path.relative_to(repo).as_posix()
        elif path.is_relative_to(template):
            label = "seed/" + path.relative_to(template).as_posix()
        else:
            label = "evals/" + path.relative_to(ROOT).as_posix()
        digest.update(label.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:20]


def _validate_inputs(repo: Path, template: Path, template_manifest: Path) -> None:
    missing = [path for path in (repo / "main.pyw", template, template_manifest) if not path.exists()]
    if missing:
        shown = "\n".join(f"  - {path}" for path in missing)
        raise FileNotFoundError(
            "Second Brain payload inputs are missing:\n"
            f"{shown}\nRun `python build_template.py --profile bench` first."
        )


def _ignore_generated(_path: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__", ".pytest_cache", ".hypothesis"}
    return {name for name in names if name in ignored or name.endswith((".pyc", ".pyo"))}


if __name__ == "__main__":
    print(prepare_payload(force=True))
