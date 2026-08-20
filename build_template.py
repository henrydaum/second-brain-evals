"""Build a golden data tree from Second Brain's general-purpose Docker image.

The template is the half of a benchmark image that cannot be pip-installed:
config plus the store packages a profile needs. It is built by *really*
installing them — the package manager copies from `origin/store` and the
ledger records the store commit and a SHA per file — so the template carries
its own provenance rather than a claim about it.

Built inside Linux on purpose.  The main repository's Dockerfile is now the
single application image; this script builds it at the requested checkout and
uses its normal one-off-command entrypoint to install the benchmark profile.

No secret is written. Keys arrive at run time through the entrypoint, because
this directory is about to become an image layer.

    python build_template.py --profile bench
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCKER = os.environ.get(
    "SB_DOCKER", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
BASE_IMAGE = "second-brain:eval-base"

#: What each profile installs. A benchmark's reported configuration is this
#: list plus the two commits below it, and nothing else.
PROFILES = {
    "bench": {
        "packages": ["bundle_essentials", "frontend_http"],
        # A service is not loaded because it is installed. These are the
        # *registered* names (a class's ``name``), not file stems.
        "autoload_services": ["web_search_provider"],
    },
    "minimal": {
        "packages": ["llm_litellm", "frontend_http", "tool_read_file",
                     "tool_edit_file", "tool_run_command", "tool_run_script"],
        "autoload_services": [],
    },
}

# Runs inside the container, where the kernel is importable and the store is
# reachable. Kept as a string so the builder stays one file.
INSIDE = r'''
import json, subprocess, sys, time
sys.path.insert(0, "/app")
from bundled.commands.helpers import package_manager as pm
from config import config_manager

packages = json.loads(sys.argv[1])
services = json.loads(sys.argv[2])
installed = []
for stem in packages:
    result = pm.install_package("/app", stem)
    if not result.ok:
        print("FAILED:", stem, getattr(result, "lines", ""), flush=True)
        raise SystemExit(1)
    installed.append(stem)
    print("installed:", stem, flush=True)

config = config_manager.load()
config["enabled_frontends"] = ["http"]
config["autoload_services"] = sorted(
    set(config.get("autoload_services") or []) | set(services))
config_manager.save(config)

commit = subprocess.run(["git", "-C", "/app", "rev-parse", "origin/store"],
                        capture_output=True, text=True).stdout.strip()
head = subprocess.run(["git", "-C", "/app", "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
manifest = {"packages": installed, "autoload_services": services,
            "store_commit": commit, "kernel_commit": head,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
open("/data/template_manifest.json", "w").write(json.dumps(manifest, indent=2))
print("MANIFEST", json.dumps(manifest), flush=True)
'''


def docker(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    docker_dir = str(Path(DOCKER).resolve().parent)
    env["PATH"] = docker_dir + os.pathsep + env.get("PATH", "")
    return subprocess.run([DOCKER, *args], text=True, env=env,
                          capture_output=capture, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="bench", choices=sorted(PROFILES))
    parser.add_argument("--repo", default=r"Z:\My Code\Second Brain",
                        help="the kernel checkout whose .git knows the store")
    parser.add_argument("--out", default=str(HERE / "template"))
    parser.add_argument("--image", default=BASE_IMAGE)
    parser.add_argument("--skip-image-build", action="store_true")
    options = parser.parse_args()

    spec = PROFILES[options.profile]
    out = Path(options.out).resolve()
    repo = Path(options.repo).resolve()
    if not (repo / ".git").exists():
        print(f"no git directory at {repo / '.git'}", file=sys.stderr)
        return 1

    if not options.skip_image_build:
        print(f"building {options.image} from {repo}", flush=True)
        built = docker(
            "build", "--build-arg", "PYTHON_VERSION=3.13",
            "-t", options.image, str(repo))
        if built.returncode != 0:
            print("Second Brain image build failed", file=sys.stderr)
            return built.returncode

    staging = out.with_name(out.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    print(f"building '{options.profile}' template into {out}")
    result = docker(
        "run", "--rm",
        # Store dependencies are validated during installation, but the
        # portable seed must not carry one container's Python site-packages.
        # Keep them off the Windows bind mount entirely.
        "-e", "PYTHONUSERBASE=/tmp/sb-python",
        "-v", f"{staging}:/data",
        options.image, "python", "-c", INSIDE,
        json.dumps(spec["packages"]), json.dumps(spec["autoload_services"]))
    if result.returncode != 0:
        print("template build failed", file=sys.stderr)
        return result.returncode


    # Runtime packages are installed once per Harbor environment for that
    # environment's Python.  Keeping Linux 3.13 site-packages here would make
    # the supposedly portable seed interpreter-specific.
    shutil.rmtree(staging / "python", ignore_errors=True)
    if out.exists():
        shutil.rmtree(out)
    staging.replace(out)

    manifest = out / "template_manifest.json"
    if manifest.exists():
        print("\n" + manifest.read_text(encoding="utf-8"))
    files = sum(1 for _ in (out / "Second Brain" / "installed").rglob("*.py"))
    print(f"installed python files: {files}")
    print("\nNow: python run_terminal_bench.py --smoke "
          "terminal-bench/openssl-selfsigned-cert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
