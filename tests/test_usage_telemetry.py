import json
from pathlib import Path

from driver.collect import llm_usage


def test_llm_usage_keeps_unknown_tokens_explicit(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.jsonl"
    rows = [
        {"ok": True, "prompt_tokens": 120, "duration_s": 1.25},
        {"ok": True, "prompt_tokens": 180, "duration_s": 2.0},
        {"ok": False, "prompt_tokens": None, "duration_s": 0.5},
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n",
        encoding="utf-8",
    )

    result = llm_usage(path)

    assert result == {
        "calls": 3,
        "successful_calls": 2,
        "failed_calls": 1,
        "calls_with_prompt_tokens": 2,
        "prompt_tokens_total_known": 300,
        "duration_s_total": 3.75,
    }


def test_missing_usage_file_is_zero_calls_but_not_zero_tokens(tmp_path: Path) -> None:
    result = llm_usage(tmp_path / "missing.jsonl")

    assert result["calls"] == 0
    assert result["prompt_tokens_total_known"] is None
