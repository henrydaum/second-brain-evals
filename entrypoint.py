"""Bring a benchmark container up: seed DATA_DIR, wire the model, start.

Everything secret arrives as an environment variable and is written into the
config at run time. Nothing is baked: an image layer is immutable and gets
distributed, so a key committed into one outlives any later deletion of it.

The template is copied rather than mounted at DATA_DIR because a volume
mounted over a baked path shadows it, and because every trial wants a pristine
writable copy without rebuilding the image.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path("/opt/sb-template")
DATA_HOME = Path(os.environ.setdefault("XDG_DATA_HOME", "/data"))
DATA_DIR = DATA_HOME / "Second Brain"


def seed() -> None:
    """A pristine DATA_DIR per container, when a template was baked in."""
    if DATA_DIR.exists() or not TEMPLATE.exists():
        return
    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE / "Second Brain", DATA_DIR)


def configure() -> None:
    """Write what the run was told, over whatever the template carried."""
    from config import config_manager

    config = config_manager.load()
    config["enabled_frontends"] = ["http"]
    if token := os.environ.get("SB_HTTP_TOKEN"):
        config["secret_http_token"] = token
    if port := os.environ.get("SB_HTTP_PORT"):
        config["http_port"] = int(port)

    key = os.environ.get("SB_LLM_API_KEY")
    model = os.environ.get("SB_LLM_MODEL")
    if key and model:
        profiles = dict(config.get("llm_profiles") or {})
        profiles[model] = {
            "llm_endpoint": os.environ.get("SB_LLM_ENDPOINT", ""),
            "secret_llm_api_key": key,
            "llm_context_size": int(os.environ.get("SB_LLM_CONTEXT", "0")),
            "llm_service_class": os.environ.get("SB_LLM_BACKEND",
                                                "LiteLLMService"),
            "llm_capabilities": {"image": False, "audio": False,
                                 "video": False},
        }
        config["llm_profiles"] = profiles
        config["default_llm_profile"] = model

    # A service is loaded because config says so: the kernel autoloads only
    # its own two, and an installed extension service sits there unloaded
    # until something names it. `sdk.services.call` does not load one on
    # demand, so a tool whose service is missing fails at the moment the
    # agent tries to use it — which in a benchmark reads as the model being
    # bad at the task.
    if services := os.environ.get("SB_AUTOLOAD_SERVICES"):
        wanted = [s for s in services.split(",") if s.strip()]
        current = list(config.get("autoload_services") or [])
        config["autoload_services"] = current + [s for s in wanted
                                                 if s not in current]

    # Deliverables live outside DATA_DIR: a benchmark hands the agent a task
    # directory and reads what it leaves there. Without this the agent can
    # write only its own workspace and every task write raises a dialog.
    #
    # The directories are *created*, because listing one that does not exist
    # grants nothing: the agent's first deliverable write fails, and in a
    # benchmark that reads as the model failing the task rather than as the
    # harness never having made the folder. Best-effort -- an unwritable
    # parent is the run's problem to report, not this function's to raise on.
    if extra := os.environ.get("SB_WRITABLE_DIRS"):
        writable = [p for p in extra.split(",") if p.strip()]
        config["fs_writable_dirs"] = writable
        for path in writable:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"[entrypoint] could not create {path}: {e}", flush=True)

    config_manager.save(config)
    print(f"[entrypoint] services={config.get('autoload_services')} "
          f"frontends={config['enabled_frontends']} "
          f"model={config.get('default_llm_profile') or '(none)'} "
          f"writable={config.get('fs_writable_dirs')}", flush=True)


def main() -> int:
    seed()
    sys.path.insert(0, "/app")
    os.chdir("/app")
    configure()
    return subprocess.call([sys.executable, "main.py"])


if __name__ == "__main__":
    raise SystemExit(main())
