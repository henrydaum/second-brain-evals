"""Configure one ephemeral eval data tree, then run Second Brain.

The general-purpose image's entrypoint has already seeded ``/data`` before it
executes this file. This wrapper adds only values that cannot be baked into a
distributable image: model credentials, the HTTP token, the task root, and the
job's plugin set.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import time
from pathlib import Path

#: Where the effective plugin manifest is written for collection. ``/work/live``
#: is already copied out by ``collect_container``, so the record of what this
#: container actually ran arrives beside the trial with no extra plumbing.
PROFILE_RECORD = Path("/work/live/profile.json")


def _stems(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _installed_root() -> Path:
    """Where the package manager puts installed store files.

    A function rather than a constant so the profile logic can be exercised
    off-container, where ``/data`` does not exist.
    """
    return Path("/data/Second Brain/installed")


def apply_profile() -> dict:
    """Install and remove packages so this container matches the job's profile.

    The seed image carries one plugin set; a job asks for a delta on top of it.
    Doing that here rather than baking an image per profile means comparing
    plugin configurations costs a container start instead of two builds, which
    is what makes the comparison worth running at all.

    **A failure here is fatal, deliberately.** Raising kills the container and
    the task is recorded as a harness error. The alternative -- carrying on
    with the seed's plugin set while the job's manifest claims otherwise --
    produces a trial whose configuration column is a lie, and a lie in that
    column silently corrupts every comparison drawn from it afterwards.
    """
    sys.path.insert(0, "/app")
    from bundled.commands.helpers import package_manager as pm

    add, remove = _stems("SB_ADD_PACKAGES"), _stems("SB_REMOVE_PACKAGES")
    for stem in add:
        result = pm.install_package("/app", stem)
        if not result.ok:
            raise RuntimeError(
                f"profile install failed for {stem!r}: {getattr(result, 'lines', '')}")
    for stem in remove:
        result = pm.uninstall_package(stem)
        if not result.ok:
            raise RuntimeError(
                f"profile removal failed for {stem!r}: {getattr(result, 'lines', '')}")

    # The effective manifest: what is on disk now, not what was requested.
    # A removal that silently no-ops (a stem that was never in the seed) has
    # to be visible, and only the filesystem can say.
    root = _installed_root()
    files = sorted(
        str(path.relative_to(root)).replace(os.sep, "/")
        for path in root.rglob("*.py") if not path.name.startswith("__")
    ) if root.is_dir() else []
    seed = {}
    try:
        seed = json.loads(Path(os.environ.get(
            "SB_TEMPLATE_MANIFEST", "/seed/template_manifest.json")
        ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    record = {
        "profile": os.environ.get("SB_PROFILE") or "bench",
        "requested_add": add, "requested_remove": remove,
        "installed_files": files,
        "tool_names": sorted(name[len("tool_"):-len(".py")]
                             for name in (path.rsplit("/", 1)[-1] for path in files)
                             if name.startswith("tool_")),
        "seed_packages": seed.get("packages"),
        "seed_excluded_packages": seed.get("excluded_packages"),
        "store_commit": seed.get("store_commit"),
        "kernel_commit": seed.get("kernel_commit"),
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    PROFILE_RECORD.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_RECORD.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if add or remove:
        print(f"[eval-entrypoint] profile={record['profile']} "
              f"+{','.join(add) or '-'} -{','.join(remove) or '-'} "
              f"tools={len(record['tool_names'])}", flush=True)
    return record


def configure() -> None:
    sys.path.insert(0, "/app")
    from config import config_manager

    config = config_manager.load()
    config["enabled_frontends"] = ["http"]

    token = os.environ.get("SB_HTTP_TOKEN")
    if not token:
        raise RuntimeError("SB_HTTP_TOKEN is required for an eval container")
    config["secret_http_token"] = token
    config["http_port"] = int(os.environ.get("SB_HTTP_PORT", "8787"))

    key = os.environ.get("SB_LLM_API_KEY")
    model = os.environ.get("SB_LLM_MODEL")
    endpoint = os.environ.get("SB_LLM_ENDPOINT")
    if not all((key, model, endpoint)):
        missing = [
            name
            for name, value in (
                ("SB_LLM_API_KEY", key),
                ("SB_LLM_MODEL", model),
                ("SB_LLM_ENDPOINT", endpoint),
            )
            if not value
        ]
        raise RuntimeError("missing eval model setting(s): " + ", ".join(missing))

    profiles = dict(config.get("llm_profiles") or {})
    profiles[model] = {
        "llm_endpoint": endpoint,
        "secret_llm_api_key": key,
        "llm_context_size": int(os.environ.get("SB_LLM_CONTEXT", "0")),
        "llm_service_class": os.environ.get("SB_LLM_BACKEND", "LiteLLMService"),
        "llm_capabilities": {"image": False, "audio": False, "video": False},
    }
    config["llm_profiles"] = profiles
    config["default_llm_profile"] = model

    services = [
        item.strip()
        for item in os.environ.get("SB_AUTOLOAD_SERVICES", "").split(",")
        if item.strip()
    ]
    current = list(config.get("autoload_services") or [])
    config["autoload_services"] = current + [item for item in services if item not in current]

    writable = [
        item.strip()
        for item in os.environ.get("SB_WRITABLE_DIRS", "/work/harnessbench").split(",")
        if item.strip()
    ]
    for item in writable:
        Path(item).mkdir(parents=True, exist_ok=True)
    config["fs_writable_dirs"] = writable
    config_manager.save(config)

    print(
        "[eval-entrypoint] "
        f"model={model} frontends=http writable={','.join(writable)}",
        flush=True,
    )


def main() -> None:
    # Packages first: ``configure`` writes config that a newly installed
    # frontend or service may need to be listed in, and the server reads that
    # config once at start.
    apply_profile()
    configure()
    sys.path.insert(0, "/opt/sb-evals")
    from harness.eval_telemetry import install

    install()
    os.chdir("/app")
    runpy.run_path("/app/main.py", run_name="__main__")


if __name__ == "__main__":
    main()
