import json
from pathlib import Path

from driver.collect import llm_usage


def test_llm_usage_keeps_unknown_tokens_explicit(tmp_path: Path) -> None:
    path = tmp_path / "llm_usage.jsonl"
    rows = [
        {"ok": True, "prompt_tokens": 120, "cached_prompt_tokens": 100,
         "completion_tokens": 20, "duration_s": 1.25},
        {"ok": True, "prompt_tokens": 180, "cached_prompt_tokens": 0,
         "completion_tokens": 30, "duration_s": 2.0},
        {"ok": False, "prompt_tokens": None, "duration_s": 0.5},
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n",
        encoding="utf-8",
    )

    result = llm_usage(path)

    assert result["calls"] == 3
    assert result["successful_calls"] == 2
    assert result["failed_calls"] == 1
    assert result["duration_s_total"] == 3.75
    # Two of three calls answered, so the totals cover those two and the
    # ``*_complete`` flags refuse to call the record whole.
    assert result["calls_with_prompt_tokens"] == 2
    assert result["input_tokens_billed"] == 300
    assert result["output_tokens"] == 50
    assert result["input_complete"] is False
    assert result["output_complete"] is False
    # A *reported* zero is data, not a missing value: the second call really
    # did have nothing cached, and it must not be confused with "unknown".
    assert result["cached_input_tokens"] == 100
    # Billed input is a sum of whole prompts; the largest single call is the
    # only one of the two that answers "how big did the context get".
    assert result["input_tokens_largest_call"] == 180


def test_missing_usage_file_is_zero_calls_but_not_zero_tokens(tmp_path: Path) -> None:
    result = llm_usage(tmp_path / "missing.jsonl")

    assert result["calls"] == 0
    assert result["input_tokens_billed"] is None
    assert result["output_tokens"] is None
    assert result["cached_input_tokens"] is None
    # No calls at all is not a complete record either.
    assert result["input_complete"] is False
