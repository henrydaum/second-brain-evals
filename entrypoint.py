"""Configure one ephemeral eval data tree, then run Second Brain.

The general-purpose image's entrypoint has already seeded ``/data`` before it
executes this file. This wrapper adds only values that cannot be baked into a
distributable image: model credentials, the HTTP token, and the task root.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


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
    configure()
    sys.path.insert(0, "/opt/sb-evals")
    from harness.eval_telemetry import install

    install()
    os.chdir("/app")
    runpy.run_path("/app/main.py", run_name="__main__")


if __name__ == "__main__":
    main()
