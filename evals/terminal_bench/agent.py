"""Harbor adapter that runs Second Brain in Terminal-Bench task containers."""

from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import override

from harbor.agents.base import BaseAgent
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harness.payload import prepare_payload

INSTALL_ROOT = "/opt/second-brain-agent"
REMOTE_LOGS = f"{INSTALL_ROOT}/logs"
INSTRUCTION = f"{INSTALL_ROOT}/instruction.md"
PROVIDER_SENTINEL = ".provider-unavailable.json"

_PROVIDER_FAILURE_MARKERS = (
    "token plan usage limit",
    "usage limit reached",
    "insufficient_quota",
    "purchase credits",
    "authenticationerror",
    "authentication error",
    "invalid api key",
    "permissiondeniederror",
)


class ProviderUnavailableError(RuntimeError):
    """The external model provider made this trial invalid."""


class SecondBrainAgent(BaseAgent):
    """Install the framework in the environment, then drive one attended turn."""

    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=("SB_LLM_API_KEY",),
        base_url_envs=("SB_LLM_ENDPOINT",),
    )

    @staticmethod
    @override
    def name() -> str:
        return "second-brain"

    @override
    def version(self) -> str:
        manifest = prepare_payload() / "manifest.json"
        return json.loads(manifest.read_text(encoding="utf-8"))["identity"]

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        payload = prepare_payload()
        await environment.exec(f"mkdir -p {INSTALL_ROOT}", user="root", timeout_sec=30)
        await environment.upload_dir(payload, INSTALL_ROOT)

        install = (
            "set -eu; "
            "command -v python3 >/dev/null 2>&1 || "
            "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv); "
            f"python3 -m venv {INSTALL_ROOT}/venv || "
            "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv && "
            f"python3 -m venv {INSTALL_ROOT}/venv); "
            f"{INSTALL_ROOT}/venv/bin/python -m pip install --disable-pip-version-check "
            f"-r {INSTALL_ROOT}/requirements.txt"
        )
        result = await environment.exec(install, user="root", timeout_sec=600)
        if result.return_code:
            raise RuntimeError(
                "Second Brain dependency installation failed: "
                + (result.stderr or result.stdout or "unknown error")[-4000:]
            )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        cwd_result = await environment.exec("pwd", timeout_sec=30)
        if cwd_result.return_code:
            raise RuntimeError(cwd_result.stderr or "could not determine task workdir")
        project = (cwd_result.stdout or "").strip()
        if not project:
            raise RuntimeError("task workdir was empty")

        local_instruction = self.logs_dir / "instruction.md"
        local_instruction.parent.mkdir(parents=True, exist_ok=True)
        local_instruction.write_text(instruction, encoding="utf-8")
        await environment.upload_file(local_instruction, INSTRUCTION)
        await environment.exec(f"mkdir -p {REMOTE_LOGS}", user="root", timeout_sec=30)

        connection = self.model_connection
        key = connection.api_key or self._get_env("SB_LLM_API_KEY")
        endpoint = connection.configured_base_url or self._get_env("SB_LLM_ENDPOINT")
        model = self.model_name or self._get_env("SB_LLM_MODEL")
        token = self._get_env("SB_HTTP_TOKEN") or "terminal-bench-local-token"
        missing = [name for name, value in (
            ("SB_LLM_API_KEY", key), ("SB_LLM_ENDPOINT", endpoint), ("model", model)
        ) if not value]
        if missing:
            raise RuntimeError("missing Second Brain model configuration: " + ", ".join(missing))

        env = {
            "SB_LLM_API_KEY": str(key),
            "SB_LLM_ENDPOINT": str(endpoint),
            "SB_LLM_MODEL": str(model),
            "SB_LLM_BACKEND": self._get_env("SB_LLM_BACKEND") or "LiteLLMService",
            "SB_HTTP_TOKEN": token,
            "SB_TASK_TIMEOUT": self._get_env("SB_TASK_TIMEOUT") or "900",
            "SB_STALL_TIMEOUT": self._get_env("SB_STALL_TIMEOUT") or "300",
            "SB_TOOL_CALL_LIMIT": self._get_env("SB_TOOL_CALL_LIMIT") or "100",
        }
        command = (
            f"{INSTALL_ROOT}/venv/bin/python {INSTALL_ROOT}/runtime_entry.py "
            f"--project {shlex.quote(project)} "
            f"--instruction-file {INSTRUCTION} --result-dir {REMOTE_LOGS} "
            "--task-id terminal-bench"
        )
        result = await environment.exec(command, env=env, timeout_sec=int(env["SB_TASK_TIMEOUT"]) + 240)

        try:
            await environment.download_dir(REMOTE_LOGS, self.logs_dir)
        except Exception as exc:
            self.logger.warning("could not download Second Brain logs: %s", exc)

        metadata = {"project": project, "driver_return_code": result.return_code}
        provider_failure = _provider_failure(self.logs_dir / "second-brain.log")
        if provider_failure:
            metadata["provider_failure"] = provider_failure
        result_path = self.logs_dir / "result.json"
        if result_path.exists():
            try:
                bundle = json.loads(result_path.read_text(encoding="utf-8"))
                metadata["second_brain"] = {
                    "outcome": bundle.get("outcome"),
                    "metrics": bundle.get("metrics"),
                    "model": bundle.get("model"),
                    "template": bundle.get("template"),
                }
            except ValueError:
                metadata["result_parse_error"] = True
        context.metadata = metadata
        if provider_failure:
            _record_provider_failure(self.logs_dir, provider_failure)
            raise ProviderUnavailableError(
                "model provider failed during the Second Brain trial: "
                + provider_failure
            )
        if result.return_code:
            raise RuntimeError(
                f"Second Brain driver exited {result.return_code}: "
                + (result.stderr or result.stdout or "see agent logs")[-4000:]
            )


def _provider_failure(path: Path) -> str | None:
    """Return a safe diagnostic when an external provider invalidates a trial."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        lowered = line.lower()
        if any(marker in lowered for marker in _PROVIDER_FAILURE_MARKERS):
            # Bound what Harbor puts in exception.txt while retaining the
            # provider's status/code. Credentials are never written here.
            return line.strip()[-2000:]
    return None


def _record_provider_failure(logs_dir: Path, diagnostic: str) -> Path:
    """Signal the host launcher to pause the whole job, not burn more trials."""
    job_dir = logs_dir.parents[1]
    marker = job_dir / PROVIDER_SENTINEL
    temporary = job_dir / f"{PROVIDER_SENTINEL}.{os.getpid()}.tmp"
    payload = {
        "error_type": ProviderUnavailableError.__name__,
        "trial": logs_dir.parent.name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "diagnostic": diagnostic,
    }
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return marker
