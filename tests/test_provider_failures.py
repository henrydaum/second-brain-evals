from pathlib import Path

import json

from evals.terminal_bench.agent import (
    PROVIDER_SENTINEL,
    _provider_failure,
    _record_provider_failure,
)


def test_provider_quota_failure_is_detected(tmp_path: Path) -> None:
    log = tmp_path / "second-brain.log"
    log.write_text(
        'MinimaxException - {"error":{"type":"rate_limit_error",'
        '"message":"Token Plan usage limit reached: purchase Credits (2056)"}}\n',
        encoding="utf-8",
    )

    found = _provider_failure(log)

    assert found is not None
    assert "2056" in found


def test_normal_agent_log_is_not_a_provider_failure(tmp_path: Path) -> None:
    log = tmp_path / "second-brain.log"
    log.write_text("ConversationLoop | INFO | task completed\n", encoding="utf-8")

    assert _provider_failure(log) is None


def test_missing_agent_log_is_not_a_provider_failure(tmp_path: Path) -> None:
    assert _provider_failure(tmp_path / "missing.log") is None


def test_provider_failure_signals_the_whole_job(tmp_path: Path) -> None:
    logs = tmp_path / "job" / "trial" / "agent"
    logs.mkdir(parents=True)

    marker = _record_provider_failure(logs, "quota exhausted (2056)")

    assert marker == tmp_path / "job" / PROVIDER_SENTINEL
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["error_type"] == "ProviderUnavailableError"
    assert payload["trial"] == "trial"
    assert payload["diagnostic"] == "quota exhausted (2056)"
